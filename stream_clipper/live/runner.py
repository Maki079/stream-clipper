"""One live source: recorder + chat + spike detector + clip planner -> 'cut' jobs."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from .. import log, notify
from ..config import AppConfig, SourceCfg
from ..context import Context
from ..queue import PRIORITY_LIVE
from .chat import make_chat
from .chat.base import run_chat
from .recorder import Recorder
from .spikes import ChatMessage, SpikeDetector, normalize
from .window import ClipPlanner, PlannedClip, length_limits

L = log.get("live")


def clip_id_for(source: str, start: float) -> str:
    return f"{source}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(start))}"


def top_chat(messages: list[ChatMessage], start: float, end: float, n: int = 8) -> list[dict]:
    counts: dict[str, list] = {}
    for m in messages:
        if start <= m.ts <= end:
            k = normalize(m.text)
            if k:
                counts.setdefault(k, [m.text, 0])[1] += 1
    best = sorted(counts.values(), key=lambda v: -v[1])[:n]
    return [{"text": t, "count": c} for t, c in best]


class LiveRunner:
    def __init__(self, name: str, src: SourceCfg, cfg: AppConfig, ctx: Context, *, record_chat: bool = False):
        self.name, self.src, self.cfg, self.ctx = name, src, cfg, ctx
        campaigns = [cfg.campaigns[c] for c in src.campaigns]
        self.min_len, self.max_len = length_limits(src.clip, campaigns)
        self.detector = SpikeDetector(src.spike, time.time(), max_spike_seconds=src.clip.max_seconds)
        self.planner = ClipPlanner(src.clip, self.min_len, self.max_len, src.spike.cooldown_seconds,
                                   margin=cfg.g.buffer.segment_seconds + 5)
        self.recorder = Recorder(name, src, cfg, ctx.buffer_dir(name), pinned_from=lambda: ctx.store.pending_live_start(name))
        self.recorder.on_live_change = self._on_live_change
        self.recent: deque[ChatMessage] = deque()  # last few minutes, for sidecar "top chat"
        self.stop_event = asyncio.Event()
        self.chat_log: Path | None = (ctx.data_dir / "chatlogs" / f"{name}.jsonl") if record_chat else None
        self.stats = {"messages": 0, "spikes": 0, "clips": 0}
        self._tasks: list[asyncio.Task] = []
        self._last_stats_log = 0.0

    # ------------------------------------------------------------- callbacks
    def _on_live_change(self, live: bool) -> None:
        if live:
            self.detector.reset(time.time())  # warm-up again on every (re)start
        else:
            self._submit(self.planner.flush(), final=True)

    def on_message(self, m: ChatMessage) -> None:
        self.stats["messages"] += 1
        self.recent.append(m)
        if self.recorder.live:
            self.detector.add(m)
        if self.chat_log:
            self.chat_log.parent.mkdir(parents=True, exist_ok=True)
            with self.chat_log.open("a") as f:
                f.write(json.dumps({"ts": m.ts, "user": m.user, "text": m.text}, ensure_ascii=False) + "\n")

    # -------------------------------------------------------------- planning
    def _submit(self, clips: list[PlannedClip], final: bool = False) -> None:
        for c in clips:
            cid = clip_id_for(self.name, c.start)
            spikes = [{"start": s.start, "end": s.end, "peak_messages": s.peak_messages, "peak_unique": s.peak_unique,
                       "peak_z": round(s.peak_z, 2), "baseline": round(s.baseline_messages, 2)} for s in c.spikes]
            meta = {"spikes": spikes, "top_chat": top_chat(list(self.recent), c.start, c.end + 30),
                    "chat_delay_seconds": self.src.clip.chat_delay_seconds}
            self.ctx.store.add_clip(cid, self.name, "live", c.start, c.end, c.score, list(self.src.campaigns), meta)
            self.ctx.store.update_clip(cid, score=c.score, meta_update=meta)
            self.ctx.queue.enqueue("cut", {"clip_id": cid, "final": final}, priority=PRIORITY_LIVE, dedupe_key=f"cut:{cid}")
            self.stats["clips"] += 1
            L.info("clip queued", extra={"source": self.name, "clip_id": cid, "len": round(c.end - c.start, 1),
                                         "score": c.score, "spikes": len(c.spikes)})

    def _pin_open(self, c: PlannedClip) -> None:
        """Register the open clip as pending right away so its buffer segments are pinned."""
        cid = clip_id_for(self.name, c.start)
        if not self.ctx.store.add_clip(cid, self.name, "live", c.start, c.end, c.score, list(self.src.campaigns), {}):
            self.ctx.store.c.execute("UPDATE clips SET end_ts=? WHERE id=?", (c.end, cid))

    async def _tick_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            now = time.time()
            while self.recent and self.recent[0].ts < now - 600:
                self.recent.popleft()
            if not self.recorder.live:
                continue
            spike = self.detector.tick(now)
            if spike:
                self.stats["spikes"] += 1
                kind, clip = self.planner.on_spike(spike)
                L.info("spike", extra={"source": self.name, "kind": kind, "peak_messages": spike.peak_messages,
                                       "z": round(spike.peak_z, 1), "baseline": round(spike.baseline_messages, 1)})
                if clip:
                    self._pin_open(clip)
                if kind == "new":
                    self._spike_alert(spike)
            if now - self._last_stats_log >= 60:
                self._last_stats_log = now
                L.info("chat stats", extra={"source": self.name, **self.detector.last})
            self._submit(self.planner.ready(now))

    def _spike_alert(self, s) -> None:
        n = self.cfg.g.notifications
        if not n.spike_alerts or n.backend == "none" or self.ctx.dry_run:
            return
        ratio = s.peak_messages / max(s.baseline_messages, 1)
        body = (f"{s.peak_messages} msgs/10s vs average {s.baseline_messages:.0f} ({ratio:.1f}x)\n"
                + ", ".join(f"{t} x{c}" for t, c in s.top_messages[:3]) + "\nclip is being made…")
        asyncio.get_running_loop().run_in_executor(
            None, lambda: notify.send(n, f"🔥 {self.name}: chat spike", body, self.ctx.store, True))

    # ------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        self._tasks = [asyncio.create_task(self.recorder.run(), name=f"rec-{self.name}"),
                       asyncio.create_task(self._tick_loop(), name=f"tick-{self.name}")]
        if make_chat(self.src) is not None:
            self._tasks.append(asyncio.create_task(
                run_chat(lambda: make_chat(self.src), self.on_message, self.name, self.stop_event), name=f"chat-{self.name}"))
        else:
            L.warning("no chat backend: no spikes will be detected", extra={"source": self.name})
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self.stop_event.set()
        if (s := self.detector.flush()) is not None:
            self.planner.on_spike(s)
        self._submit(self.planner.flush(), final=True)
        await self.recorder.stop()
        for t in self._tasks:
            t.cancel()

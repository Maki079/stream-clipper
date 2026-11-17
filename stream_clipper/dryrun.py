"""Dry run: full pipeline on a local video (+ recorded or simulated chat), no upload, no notifications.

LIVE-style: chat log -> spike detector -> clip windows -> render -> captions -> local output
VOD-style : transcribe -> highlight scoring -> render -> captions -> local output
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from . import log
from .config import AppConfig
from .context import Context
from .live.spikes import ChatMessage, SpikeDetector
from .live.window import ClipPlanner, length_limits
from .media.render import probe
from .pipeline import HANDLERS
from .vod.ingest import _file_key

L = log.get("dryrun")


def simulate_chat(duration: float, spikes_at: list[float], *, base_rate: float = 2.0, peak_rate: float = 15.0,
                  spike_len: float = 12.0, seed: int = 0) -> list[dict]:
    """Synthetic chat log: steady chatter with bursts at the given video seconds."""
    rnd = random.Random(seed)
    lines = ["lol", "W", "what", "bro", "nah", "KEKW", "LMAO", "no way", "chat is this real", "hi chat"]
    out = []
    t = 0.0
    while t < duration:
        hot = any(s <= t < s + spike_len for s in spikes_at)
        rate = peak_rate if hot else base_rate
        t += rnd.expovariate(rate)
        out.append({"t": round(t, 2), "user": f"viewer{rnd.randrange(800)}",
                    "text": rnd.choice(lines[4:] if hot else lines) + ("!" * rnd.randrange(3) if hot else "")})
    return out


def load_chat(path: Path, offset: float | None) -> list[dict]:
    """jsonl lines with 't' (seconds into the video) or 'ts' (epoch, as written by record-chat; needs offset =
    epoch of video start)."""
    out = []
    first_ts = None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "t" not in d:
            first_ts = first_ts if first_ts is not None else d["ts"]
            d["t"] = d["ts"] - (offset if offset is not None else first_ts)
        out.append(d)
    return out


def drain(ctx: Context) -> None:
    """Run every queued job in-process until the queue is empty."""
    from .workers import run_job

    while (job := ctx.queue.claim(list(HANDLERS), "dryrun")) is not None:
        L.info("running", extra={"job_id": job.id, "type": job.type})
        run_job(ctx, job, HANDLERS)


def release_models(ctx: Context) -> None:
    """Free CTranslate2 models before interpreter exit (avoids a C++ abort at shutdown)."""
    import gc

    ctx.cache.clear()
    gc.collect()


def prepare(cfg: AppConfig, outdir: Path) -> AppConfig:
    raw = cfg.model_dump(by_alias=True)
    g = raw["global"]
    g["data_dir"] = str(outdir / "data")
    g["buffer_dir"] = str(outdir / "buffer")
    g["delivery"]["backend"] = "local"
    g["delivery"]["local_dir"] = str(outdir / "output")
    g["delivery"]["delete_work_files"] = False
    g["notifications"] = {"backend": "none"}
    g["workers"]["max_attempts"] = 1
    if not Path(g["models_dir"]).exists() or not _writable(Path(g["models_dir"])):
        g["models_dir"] = str(outdir / "models")
    return AppConfig.model_validate(raw)


def _writable(p: Path) -> bool:
    import os
    return os.access(p, os.W_OK)


def run(cfg: AppConfig, source: str, video: Path, *, outdir: Path, chat: Path | None = None, chat_offset: float | None = None,
        simulate: list[float] | None = None, vod: bool = False, threads: int = 8, warmup: float | None = None) -> list[dict]:
    if source not in cfg.sources:
        raise SystemExit(f"unknown source {source!r}; known: {sorted(cfg.sources)}")
    cfg = prepare(cfg, outdir)
    src = cfg.sources[source]
    ctx = Context.create(cfg, threads=threads, dry_run=True, name="dryrun")
    video = video.resolve()
    info = probe(video)
    duration = info["duration"]

    if vod or src.mode == "vod":
        key = _file_key(video)
        ctx.store.claim_item(key, source, str(video), video.name)
        ctx.queue.enqueue("transcribe_full", {"source": source, "key": key, "url": str(video), "title": video.name,
                                              "video": str(video)}, dedupe_key=f"dry:{key}:{random.random()}")
    else:
        # simulated bursts must clear this source's absolute minimum (big chats need big numbers)
        need = max(src.spike.min_messages, src.spike.min_unique_chatters) / src.spike.window_seconds
        peak = max(15.0, need * 2.5)
        msgs = load_chat(chat, chat_offset) if chat else simulate_chat(
            duration, simulate or [duration * 0.5], base_rate=peak / 8, peak_rate=peak)
        spike_cfg = src.spike.model_copy(update={"warmup_seconds": warmup if warmup is not None else min(
            src.spike.warmup_seconds, duration * 0.25)})
        det = SpikeDetector(spike_cfg, 0.0, max_spike_seconds=src.clip.max_seconds)
        lo, hi = length_limits(src.clip, [cfg.campaigns[c] for c in src.campaigns])
        planner = ClipPlanner(src.clip, lo, hi, spike_cfg.cooldown_seconds)
        msgs.sort(key=lambda d: d["t"])
        i, planned = 0, []
        for sec in range(int(duration) + 1):
            while i < len(msgs) and msgs[i]["t"] <= sec:
                det.add(ChatMessage(msgs[i]["t"], msgs[i].get("user", "?"), msgs[i].get("text", "")))
                i += 1
            if (s := det.tick(sec)) is not None:
                kind, _ = planner.on_spike(s)
                L.info("spike", extra={"t": s.start, "kind": kind, "peak": s.peak_messages, "z": round(s.peak_z, 1)})
            planned += planner.ready(sec)
        if (s := det.flush()) is not None:
            planner.on_spike(s)
        planned += planner.flush()
        for c in planned:
            start, end = max(0.0, c.start), min(duration, c.end)
            cid = f"dry-{source}-{int(start):05d}"
            top = {}
            for m in msgs:
                if start <= m["t"] <= end + 30:
                    top[m["text"]] = top.get(m["text"], 0) + 1
            meta = {"raw": str(video), "offset": start, "video_seconds": True, "duration": round(end - start, 3),
                    "spikes": [{"t": s.start, "peak_messages": s.peak_messages, "peak_z": round(s.peak_z, 2)} for s in c.spikes],
                    "top_chat": [{"text": k, "count": v} for k, v in sorted(top.items(), key=lambda kv: -kv[1])[:8]]}
            ctx.store.add_clip(cid, source, "live", start, end, c.score, list(src.campaigns), meta, status="cut")
            ctx.queue.enqueue("render", {"clip_id": cid}, dedupe_key=f"render:{cid}")
        L.info("dry-run planned clips", extra={"count": len(planned)})
    drain(ctx)
    release_models(ctx)
    clips = [ctx.store.get_clip(r["id"]) for r in ctx.store.c.execute("SELECT id FROM clips ORDER BY score DESC")]
    return clips

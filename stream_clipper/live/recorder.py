"""Live recorder: streamlink -> ffmpeg segment muxer (stream copy) into a rolling buffer.

Polls until the channel is live, reconnects with backoff, re-resolves the URL on every
(re)connect (YouTube /live and Kick change the underlying stream).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from .. import log
from ..config import AppConfig, SourceCfg
from . import buffer
from .resolve import stream_input

L = log.get("recorder")


class Recorder:
    def __init__(self, name: str, src: SourceCfg, cfg: AppConfig, buffer_dir: Path, pinned_from=lambda: None):
        self.name, self.src, self.cfg, self.dir = name, src, cfg, buffer_dir
        self.pinned_from = pinned_from
        self.live = False
        self.live_since: float | None = None
        self.on_live_change = None  # callback(bool)
        self._procs: list[asyncio.subprocess.Process] = []
        self._stop = asyncio.Event()
        self._session: str | None = None

    def streamlink_cmd(self, url: str) -> list[str]:
        cmd = ["streamlink", "--stdout", "--loglevel", "warning", "--stream-segment-threads", "2",
               "--hls-live-edge", "2", "--stream-timeout", "60", "--retry-open", "2"]
        if self.src.platform == "twitch":
            cmd += ["--twitch-disable-ads"]
        return cmd + [url, f"{self.src.stream_quality},best"]

    def ffmpeg_cmd(self, session: str) -> list[str]:
        seg = self.cfg.g.buffer.segment_seconds
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
                "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment", "-segment_time", str(seg),
                "-segment_format", "mpegts", "-reset_timestamps", "1",
                "-segment_list", str(self.dir / f"{session}.csv"), "-segment_list_type", "csv",
                str(self.dir / f"{session}_%06d.ts")]

    def _set_live(self, v: bool) -> None:
        if v != self.live:
            self.live = v
            self.live_since = time.time() if v else None
            L.info("stream live" if v else "stream offline", extra={"source": self.name})
            if self.on_live_change:
                self.on_live_change(v)

    async def run(self) -> None:
        backoff = 5
        poll = self.cfg.g.buffer.poll_interval_seconds
        pruner = asyncio.create_task(self._prune_loop())
        try:
            while not self._stop.is_set():
                started = time.time()
                try:
                    url = await stream_input(self.src)
                    await self._record_once(url)
                except Exception as e:  # noqa: BLE001
                    L.warning("recorder error", extra={"source": self.name, "error": str(e)[-300:]})
                self._set_live(False)
                if self._stop.is_set():
                    break
                ran = time.time() - started
                # short run = offline or failing -> poll interval; long run = stream dropped -> reconnect fast
                wait = backoff if ran > 60 else max(poll, backoff)
                backoff = 5 if ran > 60 else min(backoff * 2, 300)
                try:
                    await asyncio.wait_for(self._stop.wait(), wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            pruner.cancel()

    async def _record_once(self, url: str) -> None:
        session = uuid.uuid4().hex[:8]
        self._session = session
        r, w = os.pipe()
        sl = await asyncio.create_subprocess_exec(*self.streamlink_cmd(url), stdout=w, stderr=asyncio.subprocess.PIPE)
        ff = await asyncio.create_subprocess_exec(*self.ffmpeg_cmd(session), stdin=r, stderr=asyncio.subprocess.PIPE)
        os.close(r)
        os.close(w)
        self._procs = [sl, ff]
        watcher = asyncio.create_task(self._watch_live(session))
        try:
            done, _ = await asyncio.wait([asyncio.create_task(sl.wait()), asyncio.create_task(ff.wait())],
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            watcher.cancel()
            await self._kill()
        err = (await sl.stderr.read()).decode(errors="replace")[-400:] if sl.stderr else ""
        if "No playable streams" in err or "No streams found" in err or "is offline" in err.lower():
            L.debug("offline", extra={"source": self.name})
        elif err.strip():
            L.info("streamlink exited", extra={"source": self.name, "stderr": err.strip()})

    async def _watch_live(self, session: str) -> None:
        """Live = segments of this session are being produced (fresh files on disk). Also keeps the session's
        timeline anchored to wall-clock (see buffer.reanchor), so clips are cut where chat reacted."""
        while True:
            await asyncio.sleep(2)
            segs = [s for s in buffer.list_segments(self.dir, settle=0) if s.session == session]
            try:
                fresh = bool(segs) and time.time() - segs[-1].path.stat().st_mtime < 30
            except FileNotFoundError:
                continue
            self._set_live(fresh)
            if fresh:
                drift = buffer.reanchor(self.dir, session)
                if drift is not None and abs(drift) > 30:
                    L.warning("stream timeline jumped, re-anchored", extra={"source": self.name, "drift": round(drift, 1)})

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                buffer.prune(self.dir, self.cfg.g.buffer.retention_seconds, self.pinned_from(),
                             keep_session=self._session)
            except Exception as e:  # noqa: BLE001
                L.warning("prune failed", extra={"source": self.name, "error": str(e)})

    async def _kill(self) -> None:
        for p in self._procs:
            if p.returncode is None:
                p.terminate()
        for p in self._procs:
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                p.kill()
        self._procs = []

    async def stop(self) -> None:
        self._stop.set()
        await self._kill()

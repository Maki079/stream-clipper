"""Main process: live runners, VOD watcher, worker pool, notifications, health, shutdown."""
from __future__ import annotations

import asyncio
import signal
import time
from pathlib import Path

from . import log, notify
from .config import AppConfig
from .context import Context
from .live.runner import LiveRunner
from .queue import PRIORITY_VOD
from .vod import ingest
from .workers import WorkerPool

L = log.get("supervisor")
HEARTBEAT = "heartbeat"


def heartbeat_path(cfg: AppConfig) -> Path:
    return Path(cfg.g.data_dir) / HEARTBEAT


class Supervisor:
    def __init__(self, config_path: str, cfg: AppConfig, *, record_chat: bool = False):
        self.config_path, self.cfg = config_path, cfg
        self.ctx = Context.create(cfg, name="supervisor")
        self.pool = WorkerPool(config_path, cfg)
        self.runners = [LiveRunner(n, s, cfg, Context.create(cfg, name=f"live-{n}"), record_chat=record_chat)
                        for n, s in cfg.enabled_sources("live").items()]
        self.stop = asyncio.Event()

    # ---------------------------------------------------------------- loops
    async def vod_watch_loop(self) -> None:
        last: dict[str, float] = {}
        while not self.stop.is_set():
            for name, src in self.cfg.enabled_sources("vod").items():
                if time.time() - last.get(name, 0) < src.vod.watch_interval_seconds:
                    continue
                last[name] = time.time()
                try:
                    items = await asyncio.to_thread(ingest.list_items, src, self.cfg.g.delivery.rclone)
                except Exception as e:  # noqa: BLE001
                    L.warning("vod listing failed", extra={"source": name, "error": str(e)[-300:]})
                    continue
                new = 0
                for it in items:
                    if self.ctx.store.claim_item(it.key, name, it.url, it.title):
                        self.ctx.queue.enqueue("ingest", {"source": name, "key": it.key, "url": it.url, "title": it.title},
                                               priority=PRIORITY_VOD, dedupe_key=f"ingest:{it.key}")
                        new += 1
                if new:
                    L.info("new VOD items queued", extra={"source": name, "count": new})
            await self._sleep(60)

    async def notify_loop(self) -> None:
        n = self.cfg.g.notifications
        if n.backend == "none":
            return
        while not self.stop.is_set():
            await self._sleep(max(30, n.batch_seconds))
            try:
                # with telegram videos each clip is sent by its own 'telegram' job instead
                clips = [] if (n.backend == "telegram" and n.telegram.send_video) else self.ctx.store.unnotified_delivered()
                if clips:
                    title, body = notify.new_clips_message(clips, n.top_n)
                    if await asyncio.to_thread(notify.send, n, title, body, self.ctx.store):
                        self.ctx.store.mark_notified([c["id"] for c in clips])
                today = notify.summary_due(n, self.cfg.g.timezone, self.ctx.store.kv_get("daily_summary_date"))
                if today:
                    since = time.time() - 86400
                    failed = self.ctx.store.c.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status='failed' AND updated_at>=?", (since,)).fetchone()[0]
                    title, body = notify.daily_summary(self.ctx.store.clips_since(since), failed)
                    if await asyncio.to_thread(notify.send, n, title, body, self.ctx.store):
                        self.ctx.store.kv_set("daily_summary_date", today)
            except Exception as e:  # noqa: BLE001
                L.warning("notify loop error", extra={"error": str(e)[:300]})

    async def maintenance_loop(self) -> None:
        hb = heartbeat_path(self.cfg)
        last_purge = 0.0
        while not self.stop.is_set():
            hb.write_text(str(time.time()))
            self.pool.check()
            stale = self.ctx.queue.requeue_running(older_than=self.cfg.g.workers.stale_job_seconds)
            if stale:
                L.warning("requeued stale jobs", extra={"count": stale})
            if time.time() - last_purge > 86400:
                self.ctx.queue.purge_done()
                last_purge = time.time()
            await self._sleep(10)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), secs)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        n = self.ctx.queue.requeue_running()
        L.info("starting", extra={"live_sources": [r.name for r in self.runners],
                                  "vod_sources": list(self.cfg.enabled_sources("vod")), "requeued_jobs": n,
                                  "cpu_workers": self.cfg.g.workers.cpu_workers, "delivery": self.cfg.g.delivery.backend})
        for w in self.cfg.warnings():
            L.warning(w)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        self.pool.start()
        if self.cfg.g.llm.enabled:
            import threading

            from .vod import llm as llm_mod
            threading.Thread(target=llm_mod.ensure_model, args=(self.cfg.g.llm,), daemon=True, name="llm-pull").start()
        tasks = [asyncio.create_task(r.run(), name=f"live-{r.name}") for r in self.runners]
        tasks += [asyncio.create_task(self.vod_watch_loop()), asyncio.create_task(self.notify_loop()),
                  asyncio.create_task(self.maintenance_loop())]
        await self.stop.wait()
        L.info("shutting down: flushing open clips, finishing running jobs")
        await asyncio.gather(*(r.stop() for r in self.runners), return_exceptions=True)
        for t in tasks:
            t.cancel()
        await asyncio.to_thread(self.pool.stop, 45)
        L.info("stopped")


def run(config_path: str, cfg: AppConfig, *, record_chat: bool = False) -> None:
    asyncio.run(Supervisor(config_path, cfg, record_chat=record_chat).run())


def health(cfg: AppConfig, max_age: float = 90) -> tuple[bool, str]:
    hb = heartbeat_path(cfg)
    if not hb.exists():
        return False, "no heartbeat file yet"
    age = time.time() - float(hb.read_text() or 0)
    if age > max_age:
        return False, f"heartbeat is {age:.0f}s old"
    try:
        Context.create(cfg, name="health").store.c.execute("SELECT 1").fetchone()
    except Exception as e:  # noqa: BLE001
        return False, f"database error: {e}"
    return True, f"ok (heartbeat {age:.0f}s ago)"

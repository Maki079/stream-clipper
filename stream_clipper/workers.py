"""Worker pool: CPU lane (separate processes, thread-limited) + IO lane (threads).

Both lanes pull from the same SQLite queue, filtered by job type, so a slow upload
never occupies a CPU worker and a long render never blocks uploads.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
import traceback
from typing import Callable

from . import log
from .context import Context
from .queue import Job, PermanentError

CPU_JOB_TYPES = {"transcribe_full", "analyze_vod", "render"}
IO_JOB_TYPES = {"ingest", "cut", "deliver", "telegram"}

Handler = Callable[[Context, dict], None]
L = log.get("workers")


def default_handlers() -> dict[str, Handler]:
    from .pipeline import HANDLERS  # imported lazily: heavy deps live there

    return HANDLERS


def limit_threads(n: int) -> None:
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "CT2_INTER_THREADS"):
        os.environ[var] = str(n)


def run_job(ctx: Context, job: Job, handlers: dict[str, Handler]) -> str:
    """Run one claimed job with a heartbeat thread. Returns final status."""
    stop_hb = threading.Event()

    def beat():
        conn_q = Context.create(ctx.cfg, name=ctx.name + "-hb").queue  # own connection for this thread
        while not stop_hb.wait(15):
            conn_q.heartbeat(job.id)
        conn_q.c.close()

    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    t0 = time.time()
    try:
        handler = handlers.get(job.type)
        if handler is None:
            raise PermanentError(f"no handler for job type {job.type!r}")
        handler(ctx, job.payload)
        ctx.queue.complete(job.id)
        L.info("job done", extra={"job_id": job.id, "type": job.type, "secs": round(time.time() - t0, 1)})
        return "done"
    except PermanentError as e:
        ctx.queue.fail(job.id, f"{e}", permanent=True)
        L.error("job failed permanently", extra={"job_id": job.id, "type": job.type, "error": str(e)})
        return "failed"
    except Exception as e:  # noqa: BLE001 - every failure is retried
        status = ctx.queue.fail(job.id, f"{e}\n{traceback.format_exc()}")
        L.error("job error", extra={"job_id": job.id, "type": job.type, "error": str(e), "next": status,
                                     "attempt": job.attempts})
        return status
    finally:
        stop_hb.set()


def work_loop(ctx: Context, types: set[str], stop: threading.Event | mp.synchronize.Event,
              handlers: dict[str, Handler] | None = None, idle_sleep: float = 1.0) -> None:
    handlers = handlers if handlers is not None else default_handlers()
    while not stop.is_set():
        job = ctx.queue.claim(types, ctx.name)
        if job is None:
            stop.wait(idle_sleep)
            continue
        run_job(ctx, job, handlers)


def _cpu_main(config_path: str, index: int, threads: int, stop, dry_run: bool) -> None:
    limit_threads(threads)
    from .config import load_config

    cfg = load_config(config_path)
    log.setup_logging(cfg.g.log_level)
    ctx = Context.create(cfg, threads=threads, dry_run=dry_run, name=f"cpu-{index}")
    L.info("cpu worker started", extra={"worker": ctx.name, "threads": threads})
    try:
        work_loop(ctx, CPU_JOB_TYPES, stop)
    except KeyboardInterrupt:
        pass
    finally:
        import gc

        ctx.cache.clear()  # free whisper models cleanly before the process exits
        gc.collect()


class WorkerPool:
    def __init__(self, config_path: str, cfg, *, dry_run: bool = False):
        self.config_path = config_path
        self.cfg = cfg
        self.dry_run = dry_run
        self.mp = mp.get_context("spawn")
        self.stop_event = self.mp.Event()
        self.io_stop = threading.Event()
        self.procs: list = []
        self.io_threads: list[threading.Thread] = []

    def start(self) -> None:
        w = self.cfg.g.workers
        for i in range(w.cpu_workers):
            self.procs.append(self._spawn(i))
        for i in range(w.io_slots):
            t = threading.Thread(target=self._io_main, args=(i,), name=f"io-{i}", daemon=True)
            t.start()
            self.io_threads.append(t)

    def _spawn(self, i: int):
        p = self.mp.Process(target=_cpu_main, name=f"cpu-{i}",
                            args=(self.config_path, i, self.cfg.g.workers.threads_per_worker, self.stop_event, self.dry_run))
        p.start()
        return p

    def _io_main(self, i: int) -> None:
        ctx = Context.create(self.cfg, threads=2, dry_run=self.dry_run, name=f"io-{i}")
        work_loop(ctx, IO_JOB_TYPES, self.io_stop)

    def check(self) -> None:
        """Restart crashed CPU workers (call periodically)."""
        if self.stop_event.is_set():
            return
        for i, p in enumerate(self.procs):
            if not p.is_alive():
                L.warning("cpu worker died, restarting", extra={"worker": p.name, "exitcode": p.exitcode})
                self.procs[i] = self._spawn(i)

    def stop(self, grace: float = 50) -> None:
        self.stop_event.set()
        self.io_stop.set()
        deadline = time.time() + grace
        for p in self.procs:
            p.join(max(0.1, deadline - time.time()))
        for p in self.procs:
            if p.is_alive():
                L.warning("cpu worker did not stop in time, terminating", extra={"worker": p.name})
                p.terminate()
                p.join(5)
        for t in self.io_threads:
            t.join(max(0.1, deadline - time.time()))

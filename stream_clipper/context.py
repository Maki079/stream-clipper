"""Per-process/thread context: config + DB connection + queue + paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import AppConfig, load_config
from .queue import JobQueue


@dataclass
class Context:
    cfg: AppConfig
    queue: JobQueue
    store: db.Store
    threads: int = 4
    dry_run: bool = False
    name: str = "main"
    cache: dict = field(default_factory=dict)  # per-process caches (e.g. whisper models)

    @classmethod
    def create(cls, cfg: AppConfig | str | Path, *, threads: int | None = None, dry_run: bool = False, name: str = "main") -> Context:
        if not isinstance(cfg, AppConfig):
            cfg = load_config(cfg)
        path = db.db_path(cfg.g.data_dir)
        db.init(path)
        conn = db.connect(path)
        q = JobQueue(conn, cfg.g.workers.max_attempts, cfg.g.workers.retry_base_seconds)
        return cls(cfg=cfg, queue=q, store=db.Store(conn), threads=threads or cfg.g.workers.threads_per_worker,
                   dry_run=dry_run, name=name)

    # paths
    @property
    def data_dir(self) -> Path:
        return Path(self.cfg.g.data_dir)

    def work_dir(self, clip_id: str) -> Path:
        p = self.data_dir / "work" / clip_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def vod_dir(self, item_key: str) -> Path:
        p = self.data_dir / "vod" / item_key.replace(":", "_").replace("/", "_")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def buffer_dir(self, source: str) -> Path:
        p = Path(self.cfg.g.buffer_dir) / source
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def models_dir(self) -> Path:
        return Path(self.cfg.g.models_dir)

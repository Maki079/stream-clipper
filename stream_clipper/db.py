"""SQLite state: job queue, clips, processed VOD items, deliveries, key/value.

One file (data_dir/state.db) in WAL mode. Every process/thread opens its own
connection via connect(); writes are short transactions, so contention is low.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    priority      INTEGER NOT NULL DEFAULT 0,       -- higher first
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 5,
    run_after     REAL NOT NULL DEFAULT 0,
    worker        TEXT,
    heartbeat_at  REAL,
    error         TEXT,
    dedupe_key    TEXT UNIQUE,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, type, run_after);

CREATE TABLE IF NOT EXISTS clips (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    mode        TEXT NOT NULL,                    -- live | vod
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | cut | rendered | delivered | failed
    start_ts    REAL NOT NULL,                    -- live: wall clock epoch; vod: seconds in video
    end_ts      REAL NOT NULL,
    score       REAL NOT NULL DEFAULT 0,
    campaigns   TEXT NOT NULL DEFAULT '[]',
    meta        TEXT NOT NULL DEFAULT '{}',
    notified    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_source_status ON clips(source, status);

CREATE TABLE IF NOT EXISTS processed_items (
    key         TEXT PRIMARY KEY,                 -- e.g. youtube:<id>, file:<sha1>
    source      TEXT NOT NULL,
    url         TEXT,
    title       TEXT,
    status      TEXT NOT NULL,                    -- queued | done | failed
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    clip_id     TEXT NOT NULL,
    campaign    TEXT NOT NULL,
    location    TEXT NOT NULL,
    link        TEXT,
    created_at  REAL NOT NULL,
    PRIMARY KEY (clip_id, campaign)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | Path) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()


def db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state.db"


# --------------------------------------------------------------- clips -----


class Store:
    """Small typed helpers over the non-queue tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # clips
    def add_clip(self, clip_id: str, source: str, mode: str, start_ts: float, end_ts: float,
                 score: float, campaigns: list[str], meta: dict, status: str = "pending") -> bool:
        now = time.time()
        cur = self.c.execute(
            "INSERT OR IGNORE INTO clips(id, source, mode, status, start_ts, end_ts, score, campaigns, meta, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (clip_id, source, mode, status, start_ts, end_ts, score, json.dumps(campaigns), json.dumps(meta), now, now),
        )
        return cur.rowcount == 1

    def get_clip(self, clip_id: str) -> dict | None:
        row = self.c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return _clip(row) if row else None

    def update_clip(self, clip_id: str, *, status: str | None = None, score: float | None = None,
                    campaigns: list[str] | None = None, meta_update: dict | None = None) -> None:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise KeyError(clip_id)
        meta = clip["meta"] | (meta_update or {})
        self.c.execute(
            "UPDATE clips SET status=?, score=?, campaigns=?, meta=?, updated_at=? WHERE id=?",
            (status or clip["status"], clip["score"] if score is None else score,
             json.dumps(clip["campaigns"] if campaigns is None else campaigns), json.dumps(meta), time.time(), clip_id),
        )

    def pending_live_start(self, source: str) -> float | None:
        """Earliest start of a live clip not cut yet: buffer segments after this are pinned."""
        row = self.c.execute(
            "SELECT MIN(start_ts) FROM clips WHERE source=? AND mode='live' AND status='pending'", (source,)
        ).fetchone()
        return row[0]

    def clips_since(self, since: float, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM clips WHERE created_at>=?"
        args: list = [since]
        if status:
            q += " AND status=?"
            args.append(status)
        return [_clip(r) for r in self.c.execute(q + " ORDER BY score DESC", args)]

    def rank_percentile(self, source: str, score: float, days: int = 30) -> float:
        """Share of this source's recent clips that scored lower (0..100)."""
        row = self.c.execute(
            "SELECT SUM(score < ?), COUNT(*) FROM clips WHERE source=? AND created_at>=?",
            (score, source, time.time() - days * 86400)).fetchone()
        return round(100.0 * (row[0] or 0) / row[1], 1) if row[1] else 100.0

    def unnotified_delivered(self) -> list[dict]:
        rows = self.c.execute("SELECT * FROM clips WHERE status='delivered' AND notified=0 ORDER BY score DESC")
        return [_clip(r) for r in rows]

    def mark_notified(self, ids: list[str]) -> None:
        self.c.executemany("UPDATE clips SET notified=1 WHERE id=?", [(i,) for i in ids])

    # processed items
    def claim_item(self, key: str, source: str, url: str, title: str = "") -> bool:
        """True if this item was never seen before (and is now recorded as queued)."""
        cur = self.c.execute(
            "INSERT OR IGNORE INTO processed_items(key, source, url, title, status, created_at) VALUES (?,?,?,?,?,?)",
            (key, source, url, title, "queued", time.time()),
        )
        return cur.rowcount == 1

    def set_item_status(self, key: str, status: str) -> None:
        self.c.execute("UPDATE processed_items SET status=? WHERE key=?", (status, key))

    def item_status(self, key: str) -> str | None:
        row = self.c.execute("SELECT status FROM processed_items WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # deliveries
    def add_delivery(self, clip_id: str, campaign: str, location: str, link: str | None) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO deliveries(clip_id, campaign, location, link, created_at) VALUES (?,?,?,?,?)",
            (clip_id, campaign, location, link, time.time()),
        )

    def deliveries(self, clip_id: str) -> list[dict]:
        return [dict(r) for r in self.c.execute("SELECT * FROM deliveries WHERE clip_id=?", (clip_id,))]

    # kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, value))


def _clip(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["campaigns"] = json.loads(d["campaigns"])
    d["meta"] = json.loads(d["meta"])
    return d

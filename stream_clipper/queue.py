"""SQLite-backed persistent job queue.

- enqueue() with optional dedupe_key (same key twice -> one job)
- claim() is atomic (single UPDATE ... RETURNING inside BEGIN IMMEDIATE)
- fail() retries with exponential backoff until max_attempts
- requeue_running() at startup puts orphaned 'running' jobs back to 'queued'
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

PRIORITY_LIVE = 10
PRIORITY_VOD = 0


class PermanentError(Exception):
    """Raise from a handler to fail a job without retrying."""


@dataclass
class Job:
    id: int
    type: str
    payload: dict
    attempts: int
    max_attempts: int
    priority: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(row["id"], row["type"], json.loads(row["payload"]), row["attempts"], row["max_attempts"], row["priority"])


class JobQueue:
    def __init__(self, conn: sqlite3.Connection, max_attempts: int = 5, retry_base_seconds: float = 30):
        self.c = conn
        self.max_attempts = max_attempts
        self.retry_base = retry_base_seconds

    def enqueue(self, type_: str, payload: dict | None = None, *, priority: int = 0, delay: float = 0,
                dedupe_key: str | None = None, max_attempts: int | None = None) -> int | None:
        """Returns the job id, or None if a job with this dedupe_key already exists."""
        now = time.time()
        cur = self.c.execute(
            "INSERT OR IGNORE INTO jobs(type, payload, priority, max_attempts, run_after, dedupe_key, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (type_, json.dumps(payload or {}), priority, max_attempts or self.max_attempts, now + delay, dedupe_key, now, now),
        )
        return cur.lastrowid if cur.rowcount == 1 else None

    def claim(self, types: list[str] | set[str], worker: str) -> Job | None:
        types = list(types)
        if not types:
            return None
        now = time.time()
        marks = ",".join("?" * len(types))
        self.c.execute("BEGIN IMMEDIATE")
        try:
            row = self.c.execute(
                f"""UPDATE jobs SET status='running', worker=?, heartbeat_at=?, attempts=attempts+1, updated_at=?
                    WHERE id = (SELECT id FROM jobs WHERE status='queued' AND run_after<=? AND type IN ({marks})
                                ORDER BY priority DESC, run_after, id LIMIT 1)
                    RETURNING *""",
                (worker, now, now, now, *types),
            ).fetchone()
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        return Job.from_row(row) if row else None

    def heartbeat(self, job_id: int) -> None:
        self.c.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? AND status='running'", (time.time(), job_id))

    def complete(self, job_id: int) -> None:
        self.c.execute("UPDATE jobs SET status='done', error=NULL, updated_at=? WHERE id=?", (time.time(), job_id))

    def fail(self, job_id: int, error: str, *, permanent: bool = False) -> str:
        """Returns the new status ('queued' for retry or 'failed')."""
        row = self.c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        now = time.time()
        if permanent or row is None or row["attempts"] >= row["max_attempts"]:
            self.c.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?", (error[-4000:], now, job_id))
            return "failed"
        delay = backoff(row["attempts"], self.retry_base)
        self.c.execute(
            "UPDATE jobs SET status='queued', error=?, run_after=?, worker=NULL, updated_at=? WHERE id=?",
            (error[-4000:], now + delay, now, job_id),
        )
        return "queued"

    def requeue_running(self, older_than: float | None = None) -> int:
        """Put running jobs back. older_than=None: all of them (startup); else only stale heartbeats.

        A requeue does not count as an attempt (attempts is decremented) so restarts don't burn retries.
        """
        now = time.time()
        if older_than is None:
            cur = self.c.execute(
                "UPDATE jobs SET status='queued', worker=NULL, attempts=MAX(attempts-1,0), updated_at=? WHERE status='running'", (now,))
        else:
            cur = self.c.execute(
                "UPDATE jobs SET status='queued', worker=NULL, updated_at=? WHERE status='running' AND heartbeat_at<?",
                (now, now - older_than))
        return cur.rowcount

    def retry_failed(self, job_id: int | None = None) -> int:
        now = time.time()
        if job_id is None:
            cur = self.c.execute("UPDATE jobs SET status='queued', attempts=0, run_after=?, updated_at=? WHERE status='failed'", (now, now))
        else:
            cur = self.c.execute("UPDATE jobs SET status='queued', attempts=0, run_after=?, updated_at=? WHERE id=? AND status='failed'",
                                 (now, now, job_id))
        return cur.rowcount

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.c.execute("SELECT type, status, COUNT(*) n FROM jobs GROUP BY type, status"):
            out.setdefault(r["type"], {})[r["status"]] = r["n"]
        return out

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM jobs", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.c.execute(q, args)]

    def purge_done(self, older_than_seconds: float = 7 * 86400) -> int:
        cur = self.c.execute("DELETE FROM jobs WHERE status='done' AND updated_at<?", (time.time() - older_than_seconds,))
        return cur.rowcount


def backoff(attempt: int, base: float, cap: float = 3600) -> float:
    """attempt 1 -> base, 2 -> 2*base, 3 -> 4*base ... capped."""
    return min(cap, base * (2 ** max(0, attempt - 1)))

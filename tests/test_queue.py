import threading
import time

import pytest

from stream_clipper import db
from stream_clipper.queue import JobQueue, PermanentError, backoff
from stream_clipper.workers import work_loop


@pytest.fixture
def q(tmp_path):
    p = tmp_path / "s.db"
    db.init(p)
    return JobQueue(db.connect(p), max_attempts=3, retry_base_seconds=10)


def test_enqueue_claim_complete(q):
    jid = q.enqueue("render", {"clip": "a"})
    job = q.claim(["render"], "w1")
    assert job.id == jid and job.payload == {"clip": "a"} and job.attempts == 1
    assert q.claim(["render"], "w2") is None
    q.complete(jid)
    assert q.counts() == {"render": {"done": 1}}


def test_type_filter_and_priority(q):
    q.enqueue("deliver", {"n": 1})
    low = q.enqueue("render", {"n": 2}, priority=0)
    high = q.enqueue("render", {"n": 3}, priority=10)
    assert q.claim(["render"], "w").id == high
    assert q.claim(["render"], "w").id == low
    assert q.claim(["render"], "w") is None
    assert q.claim(["deliver"], "w") is not None


def test_dedupe(q):
    assert q.enqueue("cut", {}, dedupe_key="clip1") is not None
    assert q.enqueue("cut", {}, dedupe_key="clip1") is None


def test_delay(q):
    q.enqueue("cut", {}, delay=60)
    assert q.claim(["cut"], "w") is None


def test_retry_backoff_then_fail(q):
    jid = q.enqueue("render", {})
    for attempt in (1, 2):
        job = q.claim(["render"], "w")
        assert job.attempts == attempt
        assert q.fail(jid, "boom") == "queued"
        assert q.claim(["render"], "w") is None  # backoff in effect
        q.c.execute("UPDATE jobs SET run_after=0 WHERE id=?", (jid,))
    q.claim(["render"], "w")
    assert q.fail(jid, "boom") == "failed"
    assert q.list("failed")[0]["error"] == "boom"
    assert q.retry_failed() == 1


def test_backoff_values():
    assert [backoff(a, 30) for a in (1, 2, 3)] == [30, 60, 120]
    assert backoff(20, 30) == 3600


def test_requeue_after_restart(q):
    jid = q.enqueue("render", {})
    q.claim(["render"], "w")
    assert q.requeue_running() == 1
    job = q.claim(["render"], "w")
    assert job.id == jid and job.attempts == 1  # restart does not burn an attempt


def test_requeue_stale_only(q):
    a = q.enqueue("render", {})
    b = q.enqueue("render", {})
    q.claim(["render"], "w")
    q.claim(["render"], "w")
    q.c.execute("UPDATE jobs SET heartbeat_at=0 WHERE id=?", (a,))
    assert q.requeue_running(older_than=600) == 1
    assert q.claim(["render"], "w").id == a
    assert b


def test_concurrent_claims_never_duplicate(tmp_path):
    p = tmp_path / "s.db"
    db.init(p)
    q0 = JobQueue(db.connect(p))
    for i in range(200):
        q0.enqueue("render", {"i": i})
    got: list[int] = []
    lock = threading.Lock()

    def worker():
        q = JobQueue(db.connect(p))
        while (job := q.claim(["render"], threading.current_thread().name)) is not None:
            with lock:
                got.append(job.id)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 200 and len(set(got)) == 200



def test_run_job_handlers(tmp_path):
    from stream_clipper.config import parse_config
    from stream_clipper.context import Context

    cfg = parse_config({"global": {"data_dir": str(tmp_path)}})
    ctx = Context.create(cfg)
    seen = []
    handlers = {
        "ok": lambda c, p: seen.append(p["v"]),
        "perm": lambda c, p: (_ for _ in ()).throw(PermanentError("bad input")),
        "boom": lambda c, p: 1 / 0,
    }
    for t in ("ok", "perm", "boom"):
        ctx.queue.enqueue(t, {"v": t})
    stop = threading.Event()
    th = threading.Thread(target=work_loop, args=(ctx, {"ok", "perm", "boom"}, stop, handlers, 0.05))
    th.start()
    time.sleep(0.5)
    stop.set()
    th.join()
    counts = ctx.queue.counts()
    assert seen == ["ok"]
    assert counts["ok"] == {"done": 1}
    assert counts["perm"] == {"failed": 1}
    assert counts["boom"] == {"queued": 1}  # retried later

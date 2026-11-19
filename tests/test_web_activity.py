import json
import time

import pytest

from stream_clipper import db
from stream_clipper.config import parse_config
from stream_clipper.context import Context
from stream_clipper.live.runner import LiveRunner
from stream_clipper.queue import JobQueue
from stream_clipper.web_activity import clip_file, drive_link, source_activity


@pytest.fixture
def activity(tmp_path):
    cfg = parse_config({"global": {"data_dir": str(tmp_path / "data"),
                                   "buffer_dir": str(tmp_path / "buffer"),
                                   "delivery": {"local_dir": str(tmp_path / "output")}},
                        "sources": {name: {"mode": "live", "platform": "twitch", "url": name,
                                           "enabled": name != "paused"}
                                    for name in ["online", "offline", "paused"]}})
    path = db.db_path(cfg.g.data_dir)
    db.init(path)
    conn = db.connect(path)
    yield cfg, conn
    conn.close()


def test_activity_requires_fresh_recorder_and_service_heartbeat(activity):
    cfg, conn = activity
    store = db.Store(conn)
    snapshot = {"updated_at": time.time(), "pid": 123, "online": True, "farming": True, "open_clip": True}
    store.kv_set("source_live:online", json.dumps(snapshot))
    store.kv_set("source_live:offline", json.dumps(snapshot | {"online": False, "farming": False, "open_clip": False}))
    store.kv_set("source_live:paused", json.dumps(snapshot))
    statuses = source_activity(conn, cfg, alive=True, pid=123)
    assert statuses["online"]["farming"] and statuses["online"]["building"]
    assert statuses["offline"]["status"] == "offline"
    assert statuses["paused"]["status"] == "paused" and not statuses["paused"]["farming"]
    assert not source_activity(conn, cfg, alive=False)["online"]["farming"]
    assert source_activity(conn, cfg, alive=True, pid=999)["online"]["status"] == "unknown"
    store.kv_set("source_live:online", json.dumps(snapshot | {"updated_at": time.time() - 21}))
    assert source_activity(conn, cfg, alive=True)["online"]["status"] == "unknown"


def test_production_stats_include_clip_and_source_jobs_separately(activity):
    cfg, conn = activity
    store, queue = db.Store(conn), JobQueue(conn)
    store.add_clip("one", "online", "live", 0, 65, 8, [], {}, status="delivered")
    store.add_clip("two", "online", "live", 0, 65, 8, [], {})
    store.add_clip("old", "online", "live", 0, 65, 8, [], {}, status="delivered")
    conn.execute("UPDATE clips SET created_at=? WHERE id='old'", (time.time() - 90000,))
    queue.enqueue("render", {"clip_id": "two"})
    queue.claim(["render"], "test-worker")
    queue.enqueue("ingest", {"source": "online"})
    queue.enqueue("ingest", {"source": "offline"})
    queue.enqueue("telegram", {"clip_id": "one"})
    statuses = source_activity(conn, cfg, alive=True)
    assert statuses["online"]["clips_24h"] == 2
    assert statuses["online"]["delivered_24h"] == 1
    assert statuses["online"]["running_jobs"] == 1
    assert statuses["online"]["queued_jobs"] == 1
    assert statuses["offline"]["queued_jobs"] == 1
    assert statuses["online"]["building"]


def test_runner_publishes_observed_capture_and_stopped_state(activity):
    cfg, conn = activity
    ctx = Context.create(cfg)
    try:
        runner = LiveRunner("online", cfg.sources["online"], cfg, ctx)
        runner.recorder._set_live(True)
        value = json.loads(ctx.store.kv_get("source_live:online"))
        assert value["online"] and value["farming"]
        runner.planner.open = object()
        runner.publish_activity()
        assert json.loads(ctx.store.kv_get("source_live:online"))["open_clip"]
        runner.publish_activity(stopped=True)
        value = json.loads(ctx.store.kv_get("source_live:online"))
        assert not value["online"] and not value["open_clip"]
        runner.src.chat_backend = "none"
        runner.publish_activity()
        value = json.loads(ctx.store.kv_get("source_live:online"))
        assert value["online"] and not value["farming"]
    finally:
        ctx.store.c.close()


def test_clip_lookup_restricts_paths_and_accepts_completed_work_copy(activity, tmp_path):
    cfg, _ = activity
    output = tmp_path / "output" / "campaign"
    output.mkdir(parents=True)
    video = output / "one.mp4"
    video.write_bytes(b"video")
    assert clip_file(cfg, "one", {}, [{"link": str(video)}]) == video.resolve()
    outside = tmp_path / "one.mp4"
    outside.write_bytes(b"secret")
    assert clip_file(cfg, "one", {"video": str(outside)}, []) is None
    assert clip_file(cfg, "../one", {"video": str(video)}, []) is None
    assert clip_file(cfg, "two", {}, [{"link": str(video)}]) is None
    work = tmp_path / "data" / "work" / "one" / "one.mp4"
    work.parent.mkdir(parents=True)
    work.write_bytes(b"finished")
    assert clip_file(cfg, "one", {"video": str(work)}, []) == work.resolve()
    video.unlink()
    assert clip_file(cfg, "one", {}, [{"link": str(video)}]) is None


def test_drive_download_links_only_use_expected_https_host():
    good = "https://drive.google.com/file/d/123/view"
    assert drive_link([{"link": good}]) == good
    assert drive_link([{"link": "https://drive.google.com.evil.test/file/d/123"}]) is None
    assert drive_link([{"link": "javascript:alert(1)"}]) is None

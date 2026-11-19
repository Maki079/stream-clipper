import base64
import json
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from stream_clipper import db
from stream_clipper.config import ConfigError, load_config
from stream_clipper.queue import JobQueue
from stream_clipper.web import ConflictError, WebApp, WebServer


@pytest.fixture
def app(tmp_path):
    app = WebApp(tmp_path / "config.yaml")
    raw = app.config()["config"]
    raw["global"]["data_dir"] = str(tmp_path / "data")
    app.save(raw, "missing")
    return app


def test_first_run_does_not_create_config_or_database(tmp_path):
    app = WebApp(tmp_path / "config.yaml")
    assert not app.config()["exists"]
    assert app.status()["jobs"] == []
    assert not app.path.exists()
    assert not (tmp_path / "data").exists()


def test_validated_atomic_save_and_stale_edit(app):
    original = app.config()
    raw = original["config"]
    raw["global"]["workers"]["cpu_workers"] = 0
    before = app.path.read_bytes()
    with pytest.raises(ConfigError):
        app.save(raw, original["revision"])
    assert app.path.read_bytes() == before
    raw["global"]["workers"]["cpu_workers"] = 2
    saved = app.save(raw, original["revision"])
    assert load_config(app.path).g.workers.cpu_workers == 2
    with pytest.raises(ConflictError):
        app.save(raw, original["revision"])
    assert app.config()["revision"] == saved["revision"]
    assert not list(app.path.parent.glob(".clipper-*.tmp"))


def test_queue_history_and_selective_retry(app):
    path = db.db_path(load_config(app.path).g.data_dir)
    db.init(path)
    conn = db.connect(path)
    try:
        queue = JobQueue(conn)
        failed = queue.enqueue("render")
        running = queue.enqueue("cut")
        queue.fail(failed, "test failure", permanent=True)
        db.Store(conn).add_clip("one", "channel", "live", 0, 65, 8, [], {}, status="delivered")
        result = app.status()
        assert result["clip_count"] == 1
        assert result["clips"][0]["source"] == "channel"
        assert result["counts"] == {"failed": 1, "queued": 1}
        assert app.retry(failed) == 1
        assert app.retry(running) == 0
        assert app.status()["counts"] == {"queued": 2}
    finally:
        conn.close()


def test_saved_changes_do_not_change_active_database(app):
    app.active_config = load_config(app.path)
    saved = app.config()
    saved["config"]["global"]["data_dir"] = str(app.path.parent / "other")
    app.save(saved["config"], saved["revision"])
    assert app.status()["jobs"] == []
    assert not (app.path.parent / "other").exists()


def test_service_uses_snapshot_and_stops_gracefully(app, monkeypatch):
    class Process:
        returncode = None
        stdout = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            assert timeout == 65

    process = Process()
    commands = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: commands.append(command) or process)
    monkeypatch.setattr(app, "_read_logs", lambda process: None)
    app.start()
    snapshot = app.snapshot
    assert str(snapshot) in commands[0]
    assert snapshot != app.path
    original = load_config(snapshot).g.timezone
    saved = app.config()
    saved["config"]["global"]["timezone"] = "Europe/Warsaw"
    app.save(saved["config"], saved["revision"])
    assert load_config(snapshot).g.timezone == original
    assert app.status()["restart_required"]
    app.start()
    assert len(commands) == 1
    app.stop()
    assert process.poll() == 0
    assert not snapshot.exists()


def test_external_heartbeat_prevents_duplicate_supervisor(app):
    hb = app.path.parent / "data" / "heartbeat"
    hb.parent.mkdir()
    hb.write_text(str(time.time()))
    assert app.status()["external"]
    with pytest.raises(ConflictError):
        app.start()


@pytest.fixture
def server(app):
    server = WebServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, path, data=None, headers=None):
    headers = headers or {}
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = Request(f"http://127.0.0.1:{server.server_port}{path}", data=data, headers=headers)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


def test_http_forms_assets_and_csrf(server):
    code, body, headers = request(server, "/")
    assert code == 200 and b"Stream Clipper" in body
    assert b"app.js" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for path in ("/app.js", "/style.css"):
        assert request(server, path)[0] == 200
    assert request(server, "/../../config.yaml")[0] == 404
    result = json.loads(request(server, "/api/config")[1])
    payload = {"config": result["config"], "revision": result["revision"]}
    payload["config"]["global"]["timezone"] = "Europe/Warsaw"
    assert request(server, "/api/config", payload)[0] == 403
    csrf = {"X-Clipper-CSRF": result["csrf"]}
    assert request(server, "/api/config", payload, csrf)[0] == 200
    assert request(server, "/api/config", payload, csrf)[0] == 409
    assert request(server, "/api/service", {"action": "unknown"}, csrf)[0] == 400
    assert request(server, "/api/retry", {"id": "1 OR 1=1"}, csrf)[0] == 400


def test_http_password_host_and_health(server):
    server.password = "test-password"
    assert request(server, "/api/config")[0] == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test-password").decode()}
    assert request(server, "/api/config", headers=auth)[0] == 200
    assert request(server, "/api/config", headers=auth | {"Host": "evil.example"})[0] == 403
    assert request(server, "/health")[0] == 200
    server.allowed_hosts.add("clipper.example")
    assert request(server, "/api/status", headers=auth | {"Host": "clipper.example"})[0] == 200

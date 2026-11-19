import base64
import json
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from stream_clipper import db
from stream_clipper import web
from stream_clipper.config import ConfigError, load_config
from stream_clipper.queue import JobQueue
from stream_clipper.web import ConflictError, WebApp, WebServer, host_allowed


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


@pytest.mark.parametrize("rule,host,expected", [
    ("clipper.example.com", "CLIPPER.EXAMPLE.COM.", True),
    ("*.example.com", "clipper.example.com", True),
    ("*.example.com", "studio.dev.example.com", True),
    ("*.EXAMPLE.com.", "CLIPPER.example.COM", True),
    ("*.example.com", "example.com", False),
    ("*.example.com", "notexample.com", False),
    ("*.example.com", "example.com.evil.test", False),
    ("*.example.com", ".example.com", False),
    ("*.example.com", "x..example.com", False),
    ("192.168.1.0/24", "192.168.1.0", True),
    ("192.168.1.0/24", "192.168.1.255", True),
    ("192.168.1.10/24", "192.168.1.200", True),
    ("192.168.1.0/24", "192.168.2.1", False),
    ("192.168.1.0/24", "clipper.example.com", False),
    ("192.168.1.0/24", "::ffff:192.168.1.1", False),
    ("fd00::/8", "fd12:3456::1", True),
    ("fd00::/8", "fe80::1", False),
    ("fd00::/8", "192.168.1.1", False),
    ("2001:db8::1", "2001:0db8:0:0:0:0:0:1", True),
    ("0.0.0.0/0", "203.0.113.1", True),
    ("::/0", "2001:db8::1", True),
    ("example.com", None, False),
])
def test_host_matching(rule, host, expected):
    assert host_allowed(host, [rule]) is expected


@pytest.mark.parametrize("rule", [
    "*", "example.*", "clip*.example.com", "*.*.example.com", "*.192.168.1.1",
    "https://example.com", "example.com:8080", "example.com/path", "user@example.com",
    "192.168.1.0/33", "fd00::/129", "bad network/24", "example..com", "fe80::1%eth0",
])
def test_invalid_host_rules_rejected_at_startup(app, rule):
    with pytest.raises(ConfigError, match="Invalid CLIPPER_WEB_HOSTS entry"):
        WebServer(("127.0.0.1", 0), app, password="test", allowed_hosts=[rule])


@pytest.mark.parametrize("host,expected", [
    ("clipper.example.com:8080", 200),
    ("STUDIO.DEV.EXAMPLE.COM.:8080", 200),
    ("example.com", 403),
    ("notexample.com", 403),
    ("example.com.evil.test", 403),
    ("192.168.1.42:8080", 200),
    ("192.168.2.42", 403),
    ("[fd12:3456::1]:8080", 200),
    ("[fe80::1]", 403),
    ("attacker.test@clipper.example.com", 403),
    ("clipper.example.com/path", 403),
    ("clipper.example.com:invalid", 403),
])
def test_http_wildcards_cidr_and_host_header_validation(server, host, expected):
    server.allowed_hosts.update(["*.example.com", "192.168.1.0/24", "fd00::/8"])
    assert request(server, "/api/status", headers={"Host": host})[0] == expected


def test_wildcard_does_not_bypass_password_or_csrf(server):
    server.allowed_hosts.add("*.example.com")
    server.password = "test"
    headers = {"Host": "clipper.example.com"}
    assert request(server, "/api/config", headers=headers)[0] == 401
    headers["Authorization"] = "Basic " + base64.b64encode(b"admin:test").decode()
    assert request(server, "/api/config", headers=headers)[0] == 200
    assert request(server, "/api/retry", {}, headers)[0] == 403


def test_host_bypass_keeps_password_and_csrf_checks(server):
    headers = {"Host": "unlisted.example"}
    assert request(server, "/api/config", headers=headers)[0] == 403
    server.skip_host_check = True
    server.password = "test"
    assert request(server, "/api/config", headers=headers)[0] == 401
    headers["Authorization"] = "Basic " + base64.b64encode(b"admin:test").decode()
    code, body, _ = request(server, "/api/config", headers=headers)
    assert code == 200
    result = json.loads(body)
    assert request(server, "/", headers=headers)[0] == 200
    assert request(server, "/api/validate", {"config": result["config"]}, headers)[0] == 403
    headers["X-Clipper-CSRF"] = result["csrf"]
    assert request(server, "/api/validate", {"config": result["config"]}, headers)[0] == 200
    server.skip_host_check = False
    assert request(server, "/api/config", headers=headers)[0] == 403


def test_host_bypass_skips_header_parsing_and_rule_validation(app):
    server = WebServer(("127.0.0.1", 0), app, allowed_hosts=["not/a/network"], skip_host_check=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "/api/status", headers={"Host": "arbitrary.example/path"})[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("0", False),
                                          ("true", True), ("1", True), (" YES ", True)])
def test_host_bypass_environment_flag(monkeypatch, value, expected):
    monkeypatch.delenv("CLIPPER_WEB_SKIP_HOST_CHECK", raising=False)
    if value is not None:
        monkeypatch.setenv("CLIPPER_WEB_SKIP_HOST_CHECK", value)
    assert web.env_flag("CLIPPER_WEB_SKIP_HOST_CHECK") is expected


def test_host_bypass_environment_wired_to_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_WEB_SKIP_HOST_CHECK", "true")
    monkeypatch.setenv("CLIPPER_WEB_HOSTS", "invalid/host/rule")
    monkeypatch.delenv("CLIPPER_WEB_PASSWORD", raising=False)
    monkeypatch.setattr(WebServer, "serve_forever", lambda self: None)
    monkeypatch.setattr(web.signal, "signal", lambda *args: None)
    args = SimpleNamespace(config=tmp_path / "config.yaml", host="127.0.0.1", port=0,
                           record_chat=False, autostart=False)
    web.serve(args)  # ignores both the invalid allowlist and its password requirement
    monkeypatch.setenv("CLIPPER_WEB_SKIP_HOST_CHECK", "typo")
    with pytest.raises(ConfigError, match="CLIPPER_WEB_SKIP_HOST_CHECK must be true or false"):
        web.serve(args)
    monkeypatch.setenv("CLIPPER_WEB_SKIP_HOST_CHECK", "false")
    with pytest.raises(ConfigError, match="Set CLIPPER_WEB_PASSWORD"):
        web.serve(args)


def test_container_restart_unavailable_in_local_mode(server):
    status = json.loads(request(server, "/api/status")[1])
    assert not status["container_restart_available"]
    headers = {"X-Clipper-CSRF": server.app.csrf}
    assert request(server, "/api/container/restart", {}, headers)[0] == 403
    assert not server.container_restarting.is_set()


def test_container_restart_requires_auth_and_csrf_then_shuts_down(server):
    server.allow_container_restart = True
    server.password = "test"
    assert json.loads(request(server, "/api/status", headers={
        "Authorization": "Basic " + base64.b64encode(b"admin:test").decode()})[1])["container_restart_available"]
    assert request(server, "/api/container/restart", {})[0] == 401
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
    assert request(server, "/api/container/restart", {}, headers)[0] == 403
    assert not server.container_restarting.is_set()
    headers["X-Clipper-CSRF"] = server.app.csrf
    code, body, _ = request(server, "/api/container/restart", {}, headers)
    assert code == 202 and json.loads(body)["ok"]
    assert server.container_restarting.is_set()


def test_docker_mode_drains_service_on_web_shutdown(app, monkeypatch):
    monkeypatch.setenv("CLIPPER_WEB_CONTAINER", "true")
    monkeypatch.delenv("CLIPPER_WEB_HOSTS", raising=False)
    monkeypatch.delenv("CLIPPER_WEB_SKIP_HOST_CHECK", raising=False)
    monkeypatch.setattr(web, "WebApp", lambda *args, **kwargs: app)
    monkeypatch.setattr(web.signal, "signal", lambda *args: None)
    stopped = []
    monkeypatch.setattr(app, "stop", lambda: stopped.append(True))

    def finish(server):
        assert server.allow_container_restart
        server.container_restarting.set()

    monkeypatch.setattr(WebServer, "serve_forever", finish)
    args = SimpleNamespace(config=app.path, host="127.0.0.1", port=0,
                           record_chat=False, autostart=False)
    web.serve(args)
    assert stopped == [True]


def test_authenticated_download_streams_only_recorded_local_clip(server, tmp_path):
    app = server.app
    raw = app.config()
    output = tmp_path / "output"
    raw["config"]["global"]["delivery"]["local_dir"] = str(output)
    app.save(raw["config"], raw["revision"])
    output.mkdir()
    video = output / "one.mp4"
    payload = b"fake video content" * 10000
    video.write_bytes(payload)
    path = db.db_path(load_config(app.path).g.data_dir)
    db.init(path)
    conn = db.connect(path)
    try:
        store = db.Store(conn)
        store.add_clip("one", "channel", "live", 0, 65, 8, [], {}, status="delivered")
        store.add_delivery("one", "campaign", str(output), str(video))
        assert app.status()["clips"][0]["download_available"]
        server.password = "test"
        assert request(server, "/api/clips/one/download")[0] == 401
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test").decode()}
        code, body, headers = request(server, "/api/clips/one/download", headers=auth)
        assert code == 200 and body == payload
        assert headers["Content-Type"] == "video/mp4"
        assert headers["Content-Disposition"].startswith("attachment;")
        assert int(headers["Content-Length"]) == len(payload)
        assert request(server, "/api/clips/missing/download", headers=auth)[0] == 404
        assert request(server, "/api/clips/%2e%2e%2fone/download", headers=auth)[0] == 404
        conn.execute("UPDATE clips SET status='cut' WHERE id='one'")
        assert request(server, "/api/clips/one/download", headers=auth)[0] == 404
        conn.execute("UPDATE clips SET status='delivered' WHERE id='one'")
        video.unlink()
        assert not app.status()["clips"][0]["download_available"]
        assert request(server, "/api/clips/one/download", headers=auth)[0] == 404
    finally:
        conn.close()

"""Local administration UI and API, served without additional dependencies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .config import AppConfig, ConfigError, load_config, parse_config
from .queue import JobQueue


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


class ConflictError(Exception):
    pass


def normalize_host(host: str) -> str:
    """Canonicalize an IP literal or DNS hostname, without resolving DNS."""
    host = host.removesuffix(".").lower()
    if "%" in host:
        raise ValueError("IPv6 scope identifiers are not supported")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.encode("idna").decode("ascii")
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                     for part in host.split(".")):
            raise ValueError("invalid hostname")
        return host


def parse_host_rule(rule: str):
    try:
        rule = rule.strip()
        if "/" in rule:
            return "network", ipaddress.ip_network(rule, strict=False)
        if rule.startswith("*."):
            suffix = normalize_host(rule[2:])
            try:
                ipaddress.ip_address(suffix)
            except ValueError:
                return "subdomain", suffix
            raise ValueError("wildcards require a DNS domain")
        return "exact", normalize_host(rule)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"Invalid CLIPPER_WEB_HOSTS entry {rule!r}: {exc}. "
                          "Use a hostname, IP address, *.example.com, or IPv4/IPv6 CIDR.") from None


def host_allowed(host: str | None, rules) -> bool:
    try:
        host = normalize_host(host or "")
    except (ValueError, UnicodeError):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for rule in rules:
        kind, value = parse_host_rule(rule)
        if kind == "exact" and host == value:
            return True
        if kind == "subdomain" and host.endswith("." + value):
            return True
        if kind == "network" and address is not None and address in value:
            return True
    return False


class WebApp:
    def __init__(self, config_path: str | Path, *, record_chat: bool = False):
        self.path = Path(config_path).resolve()
        self.record_chat = record_chat
        self.lock = threading.RLock()
        self.process = None
        self.active_config = None
        self.active_revision = None
        self.snapshot = None
        self.logs = deque(maxlen=300)
        self.log_lock = threading.Lock()
        self.csrf = secrets.token_urlsafe(32)

    def config(self):
        with self.lock:
            cfg = load_config(self.path) if self.path.exists() else AppConfig()
            return {"config": cfg.model_dump(by_alias=True), "revision": revision(self.path),
                    "warnings": cfg.warnings(), "exists": self.path.exists()}

    def save(self, raw, expected):
        cfg = parse_config(raw)
        with self.lock:
            if revision(self.path) != expected:
                raise ConflictError("Settings changed in another session. Reload before saving again.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Replace within the same directory: readers never see a partial config.
            fd, name = tempfile.mkstemp(prefix=".clipper-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    yaml.safe_dump(cfg.model_dump(by_alias=True), out, sort_keys=False, allow_unicode=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            return self.config()

    def _read_logs(self, process):
        try:
            for line in process.stdout:
                with self.log_lock:
                    self.logs.append(line.rstrip()[:4000])
        finally:
            process.stdout.close()

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            cfg = load_config(self.path)
            hb = Path(cfg.g.data_dir) / "heartbeat"
            if hb.exists() and time.time() - hb.stat().st_mtime < 90:
                raise ConflictError("Another clipper service appears to be running. Stop it before starting here.")
            if self.snapshot:
                self.snapshot.unlink(missing_ok=True)
            # Respawned CPU workers must keep the active settings until Apply & restart.
            fd, name = tempfile.mkstemp(prefix=".clipper-active-", suffix=".yaml", dir=self.path.parent)
            self.snapshot = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                yaml.safe_dump(cfg.model_dump(by_alias=True), out, sort_keys=False)
            command = [sys.executable, "-u", "-m", "stream_clipper.cli", "-c", str(self.snapshot), "run"]
            if self.record_chat:
                command.append("--record-chat")
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace")
            self.active_config = cfg
            self.active_revision = revision(self.path)
            threading.Thread(target=self._read_logs, args=(self.process,), daemon=True).start()

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=65)
                except subprocess.TimeoutExpired:
                    # Do not start a second supervisor while an old one still owns jobs.
                    raise ConflictError("Service is still stopping. Wait and try again.") from None
                if self.active_config:
                    (Path(self.active_config.g.data_dir) / "heartbeat").unlink(missing_ok=True)
            if self.snapshot and (not self.process or self.process.poll() is not None):
                self.snapshot.unlink(missing_ok=True)

    def status(self):
        with self.lock:
            cfg = self.active_config or (load_config(self.path) if self.path.exists() else AppConfig())
            running = self.process is not None and self.process.poll() is None
            hb = Path(cfg.g.data_dir) / "heartbeat"
            age = max(0, time.time() - hb.stat().st_mtime) if hb.exists() else None
            result = {"running": running, "external": not running and age is not None and age < 90,
                      "healthy": running and age is not None and age < 90,
                      "exit_code": self.process.poll() if self.process else None,
                      "restart_required": running and self.active_revision != revision(self.path),
                      "counts": {}, "jobs": [], "clips": [], "clip_count": 0, "database_error": None}
            path = Path(cfg.g.data_dir) / "state.db"
            if path.exists():
                try:
                    # Monitoring must not create a database or modify its schema.
                    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
                    conn.row_factory = sqlite3.Row
                    try:
                        result["clip_count"] = conn.execute("SELECT COUNT(*) FROM clips WHERE status='delivered'").fetchone()[0]
                        result["counts"] = {r["status"]: r["n"] for r in conn.execute(
                            "SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
                        result["jobs"] = [dict(r) for r in conn.execute(
                            "SELECT id, type, status, attempts, max_attempts, error FROM jobs ORDER BY id DESC LIMIT 100")]
                        result["clips"] = [dict(r) for r in conn.execute(
                            "SELECT id, source, mode, status, score, start_ts, end_ts, created_at "
                            "FROM clips ORDER BY created_at DESC LIMIT 100")]
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    result["database_error"] = str(exc)
            with self.log_lock:
                result["logs"] = list(self.logs)
            return result

    def retry(self, job_id):
        with self.lock:
            cfg = self.active_config or load_config(self.path)
            path = Path(cfg.g.data_dir) / "state.db"
            if not path.exists():
                return 0
            conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=3, isolation_level=None)
            try:
                return JobQueue(conn).retry_failed(job_id)
            finally:
                conn.close()


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app, *, password="", allowed_hosts=()):
        self.app, self.password = app, password
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", *allowed_hosts}
        for rule in self.allowed_hosts:
            parse_host_rule(rule)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: WebServer

    def log_message(self, fmt, *args):
        pass

    def send_data(self, data, status=200, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        try:
            headers = self.headers.get_all("Host", [])
            if len(headers) != 1 or any(char.isspace() for char in headers[0]):
                raise ValueError("Invalid Host header")
            authority = urlsplit("//" + headers[0])
            # Accept only a host with an optional port, never a URL or userinfo.
            if authority.username is not None or authority.path or authority.query or authority.fragment:
                raise ValueError("Invalid Host header")
            authority.port  # validate the optional port
            host = authority.hostname
        except ValueError:
            host = None
        if not host_allowed(host, self.server.allowed_hosts):
            self.send_data({"error": "Host not allowed. Configure CLIPPER_WEB_HOSTS for remote access."}, 403)
            return False
        if self.server.password:
            expected = "Basic " + base64.b64encode(("admin:" + self.server.password).encode()).decode()
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Stream Clipper", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return True

    def do_GET(self):
        if self.path == "/health":
            self.send_data({"ok": True})
            return
        if not self.authorized():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/config":
                self.send_data(self.server.app.config() | {"csrf": self.server.app.csrf,
                                                          "schema": AppConfig.model_json_schema(by_alias=True)})
            elif path == "/api/status":
                self.send_data(self.server.app.status())
            elif path in ("/", "/app.js", "/style.css"):
                filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                mime = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript; charset=utf-8",
                        "/style.css": "text/css; charset=utf-8"}[path]
                self.send_data(files("stream_clipper").joinpath("web_static", filename).read_bytes(), content_type=mime)
            else:
                self.send_data({"error": "Not found"}, 404)
        except (ConfigError, OSError) as exc:
            self.send_data({"error": str(exc)}, 400)

    def do_POST(self):
        if not self.authorized():
            return
        if not hmac.compare_digest(self.headers.get("X-Clipper-CSRF", ""), self.server.app.csrf):
            self.send_data({"error": "Session expired. Reload this page."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("Request must be between 1 byte and 1 MB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request must be an object")
            app = self.server.app
            if self.path == "/api/config":
                self.send_data(app.save(body["config"], body["revision"]))
            elif self.path == "/api/validate":
                cfg = parse_config(body["config"])
                self.send_data({"warnings": cfg.warnings()})
            elif self.path == "/api/service":
                action = body.get("action")
                if action not in ("start", "stop", "restart"):
                    raise ValueError("Unknown service action")
                with app.lock:
                    if action == "restart":
                        load_config(app.path)
                    if action in ("stop", "restart"):
                        app.stop()
                    if action in ("start", "restart"):
                        app.start()
                self.send_data({"ok": True})
            elif self.path == "/api/retry":
                job_id = body.get("id")
                if job_id is not None and (type(job_id) is not int or job_id < 1):
                    raise ValueError("Invalid job ID")
                self.send_data({"retried": app.retry(job_id)})
            else:
                self.send_data({"error": "Not found"}, 404)
        except ConflictError as exc:
            self.send_data({"error": str(exc)}, 409)
        except (ConfigError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
            self.send_data({"error": str(exc)}, 400)


def serve(args):
    hosts = [h.strip() for h in os.environ.get("CLIPPER_WEB_HOSTS", "").split(",") if h.strip()]
    password = os.environ.get("CLIPPER_WEB_PASSWORD", "")
    if hosts and not password:
        raise ConfigError("Set CLIPPER_WEB_PASSWORD before enabling remote hosts (login: admin).")
    app = WebApp(args.config, record_chat=args.record_chat)
    server = WebServer((args.host, args.port), app, password=password, allowed_hosts=hosts)
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)
    if args.autostart and app.path.exists():
        app.start()
    print(f"Stream Clipper UI: http://localhost:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.stop()

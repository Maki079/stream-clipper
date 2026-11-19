"""Runtime source reporting and safe lookup of completed clip files."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import AppConfig


def source_activity(conn, cfg: AppConfig, *, alive: bool, pid: int | None = None) -> dict:
    now = time.time()
    snapshots = {}
    for row in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'source_live:%'"):
        try:
            value = json.loads(row["value"])
            if (isinstance(value, dict) and 0 <= now - value.get("updated_at", 0) < 20
                    and (pid is None or value.get("pid") == pid)):
                snapshots[row["key"].removeprefix("source_live:")] = value
        except (ValueError, TypeError):
            continue
    result = {}
    for name, src in cfg.sources.items():
        snapshot = snapshots.get(name) if alive and src.enabled and src.mode == "live" else None
        online = snapshot.get("online", False) if snapshot else None
        farming = bool(snapshot and snapshot.get("farming") and online)
        status = "paused" if not src.enabled else "stopped" if not alive else "video" if src.mode == "vod" else (
            "farming" if farming else "online" if online else "offline" if snapshot else "unknown")
        result[name] = {
            "status": status, "online": online, "farming": farming,
            "open_clip": bool(snapshot and snapshot.get("open_clip")),
            "live_since": snapshot.get("live_since") if snapshot else None,
            "messages": snapshot.get("messages", 0) if snapshot else 0,
            "spikes": snapshot.get("spikes", 0) if snapshot else 0,
            "messages_in_window": snapshot.get("messages_in_window", 0) if snapshot else 0,
            "clips_24h": 0, "delivered_24h": 0, "running_jobs": 0, "queued_jobs": 0,
            "source": {"enabled": src.enabled, "mode": src.mode, "platform": src.platform,
                       "url": src.url, "campaigns": src.campaigns},
        }
    for row in conn.execute("SELECT source, COUNT(*) total, SUM(status='delivered') delivered "
                            "FROM clips WHERE created_at>=? GROUP BY source", (now - 86400,)):
        if row["source"] in result:
            result[row["source"]].update(clips_24h=row["total"], delivered_24h=row["delivered"])
    for row in conn.execute(
        "SELECT COALESCE(json_extract(j.payload,'$.source'), c.source) source, j.status, COUNT(*) total "
        "FROM jobs j LEFT JOIN clips c ON c.id=json_extract(j.payload,'$.clip_id') "
        "WHERE j.status IN ('running','queued') AND j.type!='telegram' GROUP BY 1, 2"
    ):
        if row["source"] in result:
            result[row["source"]][row["status"] + "_jobs"] = row["total"]
    for activity in result.values():
        activity["building"] = activity["open_clip"] or activity["running_jobs"] > 0
    return result


def clip_file(cfg: AppConfig, clip_id: str, meta: dict, deliveries: list[dict]) -> Path | None:
    """Only serve the recorded clip filename inside the configured output/work roots."""
    if not clip_id or any(char in clip_id for char in ("/", "\\")):
        return None
    roots = [Path(cfg.g.delivery.local_dir).resolve(), (Path(cfg.g.data_dir) / "work" / clip_id).resolve()]
    candidates = [d.get("link") for d in deliveries] + [meta.get("video")]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate or "://" in candidate:
            continue
        try:
            path = Path(candidate).resolve()
            if (path.name == f"{clip_id}.mp4" and any(path.is_relative_to(root) for root in roots)
                    and path.is_file()):
                return path
        except (OSError, ValueError):
            continue
    return None


def drive_link(deliveries: list[dict]) -> str | None:
    for delivery in deliveries:
        link = delivery.get("link") or ""
        try:
            url = urlsplit(link)
            if (url.scheme == "https" and url.netloc == "drive.google.com"
                    and url.path.startswith("/file/d/")):
                return link
        except ValueError:
            continue
    return None

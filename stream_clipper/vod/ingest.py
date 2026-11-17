"""VOD discovery (watch lists) and download (yt-dlp / rclone / local)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import RcloneCfg, SourceCfg

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".flv", ".m4v", ".avi"}


@dataclass
class Item:
    key: str
    url: str
    title: str = ""


def _run(cmd: list[str], timeout: float) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-800:]}")
    return p.stdout


def _file_key(p: Path) -> str:
    st = p.stat()
    return "file:" + hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def list_items(src: SourceCfg, rclone: RcloneCfg) -> list[Item]:
    if src.platform == "local":
        p = Path(src.url)
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXT)
        files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
        if src.vod.watch:
            files = files[: src.vod.watch_max_items]
        return [Item(_file_key(f), str(f), f.name) for f in files]
    if src.platform == "gdrive":
        out = _run(["rclone", "--config", rclone.config_path, "lsjson", "--files-only", "-R", src.url], 300)
        entries = [e for e in json.loads(out) if Path(e["Path"]).suffix.lower() in VIDEO_EXT]
        entries.sort(key=lambda e: e.get("ModTime", ""), reverse=True)
        if src.vod.watch:
            entries = entries[: src.vod.watch_max_items]
        base = src.url.rstrip("/")
        return [Item(f"gdrive:{e.get('ID') or e['Path']}", f"{base}/{e['Path']}", e["Name"]) for e in entries]
    cmd = ["yt-dlp", "--flat-playlist", "-J", "--no-warnings"]
    if src.vod.watch:
        cmd += ["--playlist-end", str(src.vod.watch_max_items)]
    if src.platform == "kick":
        cmd += ["--impersonate", "chrome"]
    d = json.loads(_run(cmd + [src.url], 600))
    entries = d.get("entries") if d.get("_type") == "playlist" else [d]
    items = []
    for e in entries or []:
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue  # not finished yet; next poll will pick it up
        ie = (e.get("ie_key") or e.get("extractor_key") or d.get("extractor_key") or "url").lower()
        url = e.get("webpage_url") or e.get("url")
        if e.get("id") and url:
            items.append(Item(f"{ie}:{e['id']}", url, e.get("title") or ""))
    return items


def download(item_url: str, src: SourceCfg, dest_dir: Path, rclone: RcloneCfg) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if src.platform == "local":
        return Path(item_url)
    if src.platform == "gdrive":
        out = dest_dir / ("video" + Path(item_url).suffix.lower())
        _run(["rclone", "--config", rclone.config_path, "copyto", item_url, str(out)], 6 * 3600)
        return out
    existing = sorted(dest_dir.glob("video.*"))
    if existing:
        return existing[0]
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "-S", "vcodec:h264,res,acodec:m4a",
           "--merge-output-format", "mp4", "-o", str(dest_dir / "video.%(ext)s"), "--no-part", "--retries", "10"]
    if src.platform == "kick":
        cmd += ["--impersonate", "chrome"]
    _run(cmd + [item_url], 12 * 3600)
    files = [f for f in dest_dir.glob("video.*") if f.suffix.lower() in VIDEO_EXT]
    if not files:
        raise RuntimeError("yt-dlp finished but no video file found")
    return files[0]

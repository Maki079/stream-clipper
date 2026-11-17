"""Delivery backends: local folder (default for now) or Google Drive via rclone."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import log
from .config import DeliveryCfg

L = log.get("delivery")


def _place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".part")
    try:
        os.link(src, tmp)          # same filesystem: instant, no extra space
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def rclone_cmd(cfg: DeliveryCfg, *args: str) -> list[str]:
    return ["rclone", "--config", cfg.rclone.config_path, "--retries", "5", "--low-level-retries", "10",
            *cfg.rclone.extra_args, *args]


def _rclone(cfg: DeliveryCfg, *args: str, timeout: float = 3600) -> str:
    p = subprocess.run(rclone_cmd(cfg, *args), capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"rclone {args[0]} failed: {p.stderr[-800:]}")
    return p.stdout


def remote_dir(cfg: DeliveryCfg, campaign: str, date: str) -> str:
    return f"{cfg.rclone.remote}:{cfg.rclone.root.strip('/')}/{campaign}/{date}"


def deliver(cfg: DeliveryCfg, files: dict[str, Path], campaign: str, date: str, *,
            first_remote_video: str | None = None) -> tuple[str, str | None, str]:
    """files: {'video': mp4, 'caption': txt, 'sidecar': json}. Returns (location, link, remote_video_path).

    For rclone, extra campaigns pass first_remote_video so the video is copied server-side
    instead of uploaded again."""
    name = files["video"].stem
    if cfg.backend == "local":
        d = Path(cfg.local_dir) / campaign / date
        for f in files.values():
            _place(f, d / f.name)
        return str(d), str(d / files["video"].name), str(d / files["video"].name)

    rdir = remote_dir(cfg, campaign, date)
    rvideo = f"{rdir}/{files['video'].name}"
    if first_remote_video and first_remote_video != rvideo:
        _rclone(cfg, "copyto", first_remote_video, rvideo)
    else:
        _rclone(cfg, "copyto", str(files["video"]), rvideo)
    for k in ("caption", "sidecar"):
        _rclone(cfg, "copyto", str(files[k]), f"{rdir}/{files[k].name}", timeout=300)
    link = None
    try:
        info = json.loads(_rclone(cfg, "lsjson", "--stat", rvideo, timeout=120))
        if info.get("ID"):
            link = f"https://drive.google.com/file/d/{info['ID']}/view"  # private: only the Drive owner can open
    except Exception as e:  # noqa: BLE001
        L.warning("could not get Drive link", extra={"clip": name, "error": str(e)[:200]})
    return rdir, link, rvideo

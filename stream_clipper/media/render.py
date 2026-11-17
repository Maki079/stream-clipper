"""ffmpeg/ffprobe helpers: probe, concat live segments, extract audio, render final clip."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import log
from ..config import LayoutCfg, OutputCfg
from .layouts import build_filter, check_crops

L = log.get("render")


class MediaError(Exception):
    pass


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    L.debug("exec", extra={"cmd": " ".join(cmd)[:2000]})
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise MediaError(f"{cmd[0]} failed ({p.returncode}): {p.stderr[-2000:]}")
    return p


def probe(path: str | Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=120)
    info = json.loads(p.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    out = {"duration": dur, "has_audio": a is not None, "has_video": v is not None}
    if v:
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        out.update(width=int(v["width"]), height=int(v["height"]), vcodec=v.get("codec_name"),
                   pix_fmt=v.get("pix_fmt"), fps=(float(num) / float(den)) if float(den or 0) else 0.0)
    if a:
        out.update(acodec=a.get("codec_name"), sample_rate=int(a.get("sample_rate", 0)))
    return out


def concat_segments(segments: list[Path], out: Path) -> Path:
    """Join live .ts segments losslessly (stream copy)."""
    if not segments:
        raise MediaError("no segments to concat")
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-fflags", "+genpts", str(out)], timeout=600)
    lst.unlink(missing_ok=True)
    return out


def extract_audio(src: Path, out: Path, start: float = 0, duration: float | None = None, rate: int = 16000) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(out)]
    run(cmd, timeout=7200)
    return out


def cut_copy(src: Path, out: Path, start: float, duration: float) -> Path:
    """Fast (keyframe-inexact) cut with a little padding; exact trim happens in render()."""
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, start):.3f}", "-i", str(src),
         "-t", f"{duration:.3f}", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)],
        timeout=600)
    return out


def render(src: Path, out: Path, *, start: float, duration: float, layout: LayoutCfg, cfg: OutputCfg,
           ass_path: Path | None, threads: int, fonts_dir: str | None = None, effects: str | None = None) -> Path:
    info = probe(src)
    if not info.get("has_video"):
        raise MediaError(f"{src} has no video stream")
    problems = check_crops(layout, info["width"], info["height"])
    if problems:
        raise MediaError("; ".join(problems))
    graph = build_filter(layout, cfg, ass_path=str(ass_path) if ass_path else None, fonts_dir=fonts_dir,
                         src_w=info["width"], src_h=info["height"], effects=effects)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, start):.3f}", "-t", f"{duration:.3f}",
           "-i", str(src)]
    if not info["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a" if not info["has_audio"] else "0:a:0",
            "-c:v", "libx264", "-preset", cfg.preset, "-profile:v", "high", "-b:v", cfg.video_bitrate,
            "-maxrate", cfg.maxrate, "-bufsize", cfg.bufsize, "-pix_fmt", "yuv420p", "-g", str(cfg.fps * 2),
            "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-threads", str(threads), "-shortest", str(out)]
    run(cmd, timeout=3600)
    return out


def grab_frame(src: str, out: Path, at: float = 0) -> Path:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", src, "-frames:v", "1", str(out)],
        timeout=120)
    return out


def shrink_to_size(src: Path, dst: Path, max_bytes: int, *, duration: float, threads: int = 4) -> Path:
    """Re-encode `src` so it fits in `max_bytes` (for upload limits like Telegram's 50 MB)."""
    if src.stat().st_size <= max_bytes:
        return src
    budget = max_bytes * 8 * 0.95 / max(duration, 1.0) - 192_000  # bits/s for video, keep audio as is
    for factor in (1.0, 0.8, 0.6):
        vb = int(max(budget * factor, 500_000))
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vb), "-maxrate", str(vb), "-bufsize", str(vb * 2),
             "-pix_fmt", "yuv420p", "-threads", str(threads), "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(dst)], timeout=1800)
        if dst.stat().st_size <= max_bytes:
            return dst
    raise MediaError(f"could not shrink {src.name} below {max_bytes} bytes")

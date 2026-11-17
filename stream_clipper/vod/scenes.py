"""Scene-change times via ffmpeg's scene score (downscaled + 5 fps for speed)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np


def scene_changes(video: Path, threshold: float = 0.3, threads: int = 4) -> list[float]:
    vf = f"fps=5,scale=320:-2,select='gt(scene,{threshold})',showinfo"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-threads", str(threads), "-i", str(video), "-an",
                        "-vf", vf, "-f", "null", "-"], capture_output=True, text=True, timeout=6 * 3600)
    if p.returncode != 0:
        raise RuntimeError(f"scene detection failed: {p.stderr[-500:]}")
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", p.stderr)]


def density(times: list[float], n_seconds: int, window: int = 10) -> np.ndarray:
    hist = np.zeros(n_seconds, dtype=np.float32)
    for t in times:
        if 0 <= int(t) < n_seconds:
            hist[int(t)] += 1
    return np.convolve(hist, np.ones(window), mode="same").astype(np.float32)

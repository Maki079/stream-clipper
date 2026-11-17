"""Smart zoom + motion effects, planned in Python and rendered by ffmpeg's `perspective` filter.

Plan (per clip):
  * moments  = loud peaks in the clip's own audio (shouting / laughing / big reactions) plus the
               chat-spike moment for live clips; at most `max_zooms_per_minute`, `min_gap_seconds` apart
  * smart zoom = a fast eased punch-in (~0.2 s) at each moment, hold ~1.6 s, smooth zoom-out
  * zoom point = what matters in the layout: the facecam for `stack`, frame centre otherwise
  * motion   = slow continuous push-in over the whole clip (ken burns) and a short decaying
               camera shake on the strongest moment

`perspective` with eval=frame interpolates sub-pixel, so slow zooms don't jitter (zoompan does).
Subtitles are burned after this filter, so they never move.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from ..config import LayoutCfg, MotionCfg, OutputCfg


@dataclass
class Zoom:
    at: float          # seconds from clip start (peak onset)
    strength: float    # extra zoom, e.g. 0.15 = 115 %
    hold: float
    reason: str        # "audio" | "chat"
    score: float


def loudness(wav: Path, hop: float = 0.1) -> tuple[np.ndarray, float]:
    from ..vod.audio import frame_db, load_wav

    x, sr = load_wav(wav)
    return frame_db(x, sr, hop), hop


def pick_moments(db: np.ndarray, hop: float, duration: float, cfg: MotionCfg,
                 chat_moments: list[float] | None = None) -> list[Zoom]:
    """Greedy: strongest audio onsets (robust z of smoothed loudness) + chat moments, spaced out."""
    max_n = max(1, math.floor(duration / 60 * cfg.max_zooms_per_minute + 0.5))
    lo, hi = 1.0, duration - cfg.hold_seconds - 1.0
    cands: list[tuple[float, float, str]] = []
    for t in chat_moments or []:
        if lo <= t <= hi:
            cands.append((t, math.inf, "chat"))  # chat reaction is the reason the clip exists: always first
    if len(db) >= 10:
        k = max(1, int(0.5 / hop))
        sm = np.convolve(np.maximum(db, -60.0), np.ones(k) / k, mode="same")  # ignore digital silence
        med = np.median(sm)
        mad = max(float(np.median(np.abs(sm - med))) * 1.4826, 2.0)  # >= 2 dB: flat audio never "spikes"
        z = (sm - med) / mad
        # onset = where loudness rises into a peak: take local maxima of z
        for i in range(1, len(z) - 1):
            if z[i] >= cfg.audio_z and z[i] >= z[i - 1] and z[i] > z[i + 1]:
                # start the punch slightly before the peak, at the rise
                j = i
                while j > 0 and z[j - 1] > cfg.audio_z * 0.6 and (i - j) * hop < 0.6:
                    j -= 1
                t = j * hop
                if lo <= t <= hi:
                    cands.append((t, float(z[i]), "audio"))
    chosen: list[tuple[float, float, str]] = []
    for t, s, why in sorted(cands, key=lambda c: -c[1]):
        if len(chosen) >= max_n:
            break
        if all(abs(t - c[0]) >= cfg.min_gap_seconds for c in chosen):
            chosen.append((t, s, why))
    chosen.sort()
    return [Zoom(round(t, 2), cfg.zoom_strength, cfg.hold_seconds, why, round(min(s, 99.0), 2)) for t, s, why in chosen]


def focus_point(layout: LayoutCfg, out: OutputCfg) -> tuple[float, float]:
    """Point (output pixels) that stays fixed while zooming."""
    W, H = out.width, out.height
    if layout.type == "stack":
        return W / 2, layout.facecam_height * 0.45   # streamer's face in the facecam panel
    if layout.type == "center_crop":
        return W / 2, H * 0.42
    return W / 2, H / 2                               # blur_fit: middle of the 16:9 picture


def _num(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def _smooth(x: str) -> str:
    """smoothstep(clip(x,0,1)) as an ffmpeg expression."""
    c = f"clip({x},0,1)"
    return f"({c}*{c}*(3-2*{c}))"


def zoom_expr(zooms: list[Zoom], cfg: MotionCfg, duration: float, T: str) -> str:
    parts = ["1"]
    if cfg.ken_burns > 0 and duration > 0:
        parts.append(f"{_num(cfg.ken_burns)}*{_smooth(f'{T}/{_num(duration)}')}")
    for z in zooms:
        a, b = z.at, z.at + cfg.punch_in_seconds + z.hold
        parts.append(f"{_num(z.strength)}*({_smooth(f'({T}-{_num(a)})/{_num(cfg.punch_in_seconds)}')}"
                     f"-{_smooth(f'({T}-{_num(b)})/{_num(cfg.punch_out_seconds)}')})")
    return "(" + "+".join(parts) + ")"


def shake_expr(z: Zoom | None, cfg: MotionCfg, T: str, phase: float) -> str:
    if z is None or not cfg.shake or cfg.shake_px <= 0:
        return "0"
    d = f"({T}-{_num(z.at)})"
    return (f"gt({d},0)*lt({d},{_num(cfg.shake_seconds)})*{_num(cfg.shake_px)}"
            f"*exp(-{d}*{_num(5 / cfg.shake_seconds)})*sin({d}*{_num(2 * math.pi * cfg.shake_hz)}+{_num(phase)})")


def build_filter(zooms: list[Zoom], cfg: MotionCfg, layout: LayoutCfg, out: OutputCfg, duration: float) -> str | None:
    """`perspective=...` filter string, or None if there is nothing to do."""
    if not cfg.enabled or (not zooms and cfg.ken_burns <= 0):
        return None
    T = f"(in/{out.fps})"
    Z = zoom_expr(zooms, cfg, duration, T)
    cx, cy = focus_point(layout, out)
    strongest = max(zooms, key=lambda z: z.score) if zooms else None
    dx, dy = shake_expr(strongest, cfg, T, 0.0), shake_expr(strongest, cfg, T, 1.7)
    x0 = f"({_num(cx)}-{_num(cx)}/{Z}+{dx})"
    y0 = f"({_num(cy)}-{_num(cy)}/{Z}+{dy})"
    x1 = f"({x0}+W/{Z})"
    y2 = f"({y0}+H/{Z})"
    # sense=source: these source points are stretched onto the output corners
    opts = dict(x0=x0, y0=y0, x1=x1, y1=y0, x2=x0, y2=y2, x3=x1, y3=y2)
    return "perspective=" + ":".join(f"{k}='{v}'" for k, v in opts.items()) + ":interpolation=linear:sense=source:eval=frame"


def plan(wav: Path | None, duration: float, cfg: MotionCfg, layout: LayoutCfg, out: OutputCfg,
         chat_moments: list[float] | None = None) -> tuple[str | None, list[dict]]:
    if not cfg.enabled:
        return None, []
    zooms: list[Zoom] = []
    if cfg.smart_zoom:
        db, hop = loudness(wav) if wav is not None and wav.exists() else (np.zeros(0), 0.1)
        zooms = pick_moments(db, hop, duration, cfg, chat_moments)
    return build_filter(zooms, cfg, layout, out, duration), [asdict(z) for z in zooms]

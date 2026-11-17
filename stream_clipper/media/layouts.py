"""ffmpeg filter graphs for the three vertical layouts.

All graphs read [0:v] and end in [v] (1080x1920, yuv420p, constant fps, subtitles burned in).
"""
from __future__ import annotations

from ..config import LayoutCfg, OutputCfg


def _cover(w: int, h: int) -> str:
    """Scale to cover w x h, then centre-crop to exactly w x h."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"


def _esc(path: str) -> str:
    # filtergraph quoting: wrap in single quotes, escape quotes and colons inside
    return "'" + path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:") + "'"


def build_filter(layout: LayoutCfg, out: OutputCfg, *, ass_path: str | None = None, fonts_dir: str | None = None,
                 src_w: int = 1920, src_h: int = 1080, effects: str | None = None) -> str:
    W, H = out.width, out.height
    if layout.type == "stack":
        fc, gp = layout.facecam, layout.gameplay
        top_h = layout.facecam_height
        bot_h = H - top_h
        graph = (
            f"[0:v]split=2[fc][gp];"
            f"[fc]{fc.ffmpeg()},{_cover(W, top_h)}[top];"
            f"[gp]{gp.ffmpeg()},{_cover(W, bot_h)}[bot];"
            f"[top][bot]vstack=inputs=2[base]"
        )
    elif layout.type == "center_crop":
        if layout.main is not None:
            crop = layout.main.ffmpeg()
        else:
            crop = "crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))/2':0"
        graph = f"[0:v]{crop},{_cover(W, H)}[base]"
    else:  # blur_fit
        graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            # blur at quarter resolution (much cheaper), slight zoom, then scale back up
            f"[bgsrc]{_cover(W // 4, H // 4)},scale=iw*1.15:ih*1.15,crop={W // 4}:{H // 4},boxblur=8:2,scale={W}:{H}[bg];"
            f"[fgsrc]scale={W}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
        )
    tail = f"[base]setsar=1,fps={out.fps}"
    if effects:  # zoom/motion before subtitles, so text stays put
        tail += f",{effects}"
    tail += ",format=yuv420p"
    if ass_path:
        tail += f",ass=filename={_esc(ass_path)}"
        if fonts_dir:
            tail += f":fontsdir={_esc(fonts_dir)}"
    return graph + ";" + tail + "[v]"


def check_crops(layout: LayoutCfg, src_w: int, src_h: int) -> list[str]:
    """Human-readable problems with crop rectangles vs the actual source size."""
    problems = []
    for name in ("facecam", "gameplay", "main"):
        r = getattr(layout, name)
        if r is None:
            continue
        if r.x + r.w > src_w or r.y + r.h > src_h:
            problems.append(f"{name} crop {r.w}x{r.h}+{r.x}+{r.y} exceeds source {src_w}x{src_h}")
    return problems

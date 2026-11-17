"""Crop helper: grab a frame from a stream/video, draw proposed rectangles + a measuring grid,
render a preview of the vertical layout, and print the values to paste into config.yaml.

Usage:
  stream-clipper crop-helper https://twitch.tv/somechannel --facecam 0,0,480,360 --gameplay 0,0,1920,1080
  stream-clipper crop-helper /data/video.mp4 --at 120 --main 600,0,720,1080
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import LayoutCfg, OutputCfg, Rect
from .media.layouts import build_filter
from .media.render import grab_frame, run

COLORS = {"facecam": (255, 60, 60), "gameplay": (60, 200, 255), "main": (80, 255, 80)}


def resolve_input(src: str) -> str:
    """Local path as is; otherwise a direct media URL via streamlink (live) or yt-dlp (VOD)."""
    if Path(src).exists() or not src.startswith("http"):
        return src
    p = subprocess.run(["streamlink", "--stream-url", src, "best"], capture_output=True, text=True, timeout=60)
    if p.returncode == 0 and p.stdout.strip().startswith("http"):
        return p.stdout.strip()
    p = subprocess.run(["yt-dlp", "-g", "-f", "bv*[height<=1080]/b", src], capture_output=True, text=True, timeout=120)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip().splitlines()[0]
    raise RuntimeError(f"could not resolve a media URL for {src}: {p.stderr[-500:]}")


def parse_rect(s: str | None) -> Rect | None:
    if not s:
        return None
    x, y, w, h = (int(v) for v in s.split(","))
    return Rect(x=x, y=y, w=w, h=h)


def annotate(frame: Path, out: Path, rects: dict[str, Rect | None], grid: int = 100) -> tuple[int, int]:
    img = Image.open(frame).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    font = ImageFont.load_default()
    for x in range(0, img.width, grid):
        d.line([(x, 0), (x, img.height)], fill=(255, 255, 255, 60))
        d.text((x + 2, 2), str(x), fill=(255, 255, 0), font=font)
    for y in range(0, img.height, grid):
        d.line([(0, y), (img.width, y)], fill=(255, 255, 255, 60))
        d.text((2, y + 2), str(y), fill=(255, 255, 0), font=font)
    for name, r in rects.items():
        if r is None:
            continue
        c = COLORS[name]
        d.rectangle([r.x, r.y, r.x + r.w - 1, r.y + r.h - 1], outline=c, width=4, fill=(*c, 40))
        d.text((r.x + 8, r.y + 8), f"{name} {r.w}x{r.h}+{r.x}+{r.y}", fill=c, font=font)
    img.save(out)
    return img.width, img.height


def preview_layout(frame: Path, out: Path, layout: LayoutCfg, cfg: OutputCfg) -> Path:
    graph = build_filter(layout, cfg)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame), "-filter_complex", graph,
         "-map", "[v]", "-frames:v", "1", str(out)], timeout=60)
    return out


def main(src: str, *, at: float, outdir: Path, facecam: str | None, gameplay: str | None, main_rect: str | None,
         facecam_height: int = 700) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    frame = grab_frame(resolve_input(src), outdir / "frame.png", at)
    rects = {"facecam": parse_rect(facecam), "gameplay": parse_rect(gameplay), "main": parse_rect(main_rect)}
    w, h = annotate(frame, outdir / "frame_annotated.png", rects)
    print(f"source frame: {w}x{h}  ->  {outdir / 'frame.png'}")
    print(f"annotated:    {outdir / 'frame_annotated.png'} (grid every 100 px)")
    for name, r in rects.items():
        if r is None:
            continue
        bad = r.x + r.w > w or r.y + r.h > h
        print(f"{name:9s} ffmpeg: {r.ffmpeg()}{'   !! OUTSIDE FRAME' if bad else ''}")
    layout = None
    if rects["facecam"] and rects["gameplay"]:
        layout = LayoutCfg(type="stack", facecam=rects["facecam"], gameplay=rects["gameplay"], facecam_height=facecam_height)
    elif rects["main"]:
        layout = LayoutCfg(type="center_crop", main=rects["main"])
    if layout:
        p = preview_layout(frame, outdir / "preview_vertical.png", layout, OutputCfg())
        print(f"vertical preview: {p}")
        print("\nconfig.yaml snippet:\n    layout:")
        print(f"      type: {layout.type}")
        for name in ("facecam", "gameplay", "main"):
            r = getattr(layout, name)
            if r:
                print(f"      {name}: {{x: {r.x}, y: {r.y}, w: {r.w}, h: {r.h}}}")
        if layout.type == "stack":
            print(f"      facecam_height: {facecam_height}")
    else:
        print("\nPass --facecam and --gameplay (stack) or --main (center_crop) to preview a layout.")
    return 0

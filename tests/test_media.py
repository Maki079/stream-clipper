import subprocess

import pytest

from stream_clipper.config import LayoutCfg, OutputCfg, Rect
from stream_clipper.media.ass import Word, build_ass, chunk_words, split_lines, subtitle_center_y, words_in_range
from stream_clipper.media.layouts import build_filter, check_crops

from .conftest import ROOT


def _words(text: str, start=0.0, step=0.3, gap_after: dict | None = None):
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append(Word(t, t + step - 0.05, w))
        t += step + (gap_after or {}).get(i, 0)
    return out


def test_chunks_respect_max_words():
    chunks = chunk_words(_words("one two three four five six seven eight nine ten eleven"), 3, 5, 40)
    assert all(len(c) <= 5 for c in chunks)
    assert sum(len(c) for c in chunks) == 11


def test_chunks_break_on_pause_and_sentence():
    ws = _words("hello there. what is going on here", gap_after={})
    chunks = chunk_words(ws, 3, 5, 40)
    assert [w.text for w in chunks[0]] == ["hello", "there."]
    ws = _words("a b c d e f", gap_after={1: 1.0})
    assert [w.text for w in chunk_words(ws, 3, 5, 40)[0]] == ["a", "b"]


def test_chunks_fit_two_lines():
    ws = _words("supercalifragilistic expialidocious extraordinarily long words here")
    for c in chunk_words(ws, 3, 5, 18):
        text = " ".join(w.text for w in c)
        if len(c) > 1:
            a, b = split_lines(text) if len(text) > 18 else (text, "")
            assert len(a) <= 18 and len(b) <= 18


def test_split_lines_balanced():
    assert split_lines("one two three four") == ("one two", "three four")


def test_subtitle_never_covers_facecam():
    lay = LayoutCfg(type="stack", facecam=Rect(x=0, y=0, w=10, h=10), gameplay=Rect(x=0, y=0, w=10, h=10), facecam_height=700)
    y = subtitle_center_y(lay)
    # two lines at size 80 + outline ~ 220 px tall block -> top must stay below the seam
    assert y - 110 > 700
    assert subtitle_center_y(LayoutCfg(type="blur_fit")) < 1920 * 0.8
    assert subtitle_center_y(LayoutCfg(type="center_crop", subtitle_y=0.5)) == 960


def test_build_ass_structure():
    out = OutputCfg()
    ass = build_ass(_words("we did it chat no way this is crazy {bad}"), out, LayoutCfg(type="blur_fit"))
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Montserrat ExtraBold" in ass
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 10  # one per word with active-word highlight
    assert "WAY THIS IS\\NCRAZY" in ass.replace("{\\c&H0000E6FF&}", "").replace("{\\c&H00FFFFFF&}", "")
    assert "{BAD}" not in ass  # braces escaped
    assert "\\c&H0000E6FF&" in ass  # #FFE600 in ASS BGR


def test_build_ass_no_highlight_lines():
    out = OutputCfg(highlight_active_word=False, subtitle_uppercase=False)
    ass = build_ass(_words("one two three four five six"), out, LayoutCfg())
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 2
    assert events[0].split(",")[1] == "0:00:00.00"


def test_words_in_range_shifts():
    segs = [{"words": [{"start": 9.0, "end": 9.5, "word": "x"}, {"start": 10.5, "end": 11.0, "word": "y"}]}]
    assert words_in_range(segs, 10, 20) == [Word(0.5, 1.0, "y")]


def test_filters():
    out = OutputCfg()
    stack = LayoutCfg(type="stack", facecam=Rect(x=1400, y=700, w=480, h=360), gameplay=Rect(x=0, y=0, w=1920, h=1080))
    f = build_filter(stack, out, ass_path="/data/work/a b/subs.ass", fonts_dir="/app/fonts")
    assert "crop=480:360:1400:700" in f and "vstack" in f and "crop=1080:700" in f and "crop=1080:1220" in f
    assert "ass=filename='/data/work/a b/subs.ass':fontsdir='/app/fonts'" in f
    assert f.endswith("[v]")
    assert "overlay" in build_filter(LayoutCfg(type="blur_fit"), out)
    assert "ih*9/16" in build_filter(LayoutCfg(type="center_crop"), out)
    assert check_crops(stack, 1280, 720)


@pytest.mark.ffmpeg
def test_render_all_layouts(tmp_path):
    from stream_clipper.media.render import probe, render

    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(src)], check=True)
    out = OutputCfg(preset="ultrafast")
    ass = tmp_path / "s.ass"
    ass.write_text(build_ass(_words("hello chat this is a test"), out, LayoutCfg()))
    layouts = [LayoutCfg(type="blur_fit"), LayoutCfg(type="center_crop"),
               LayoutCfg(type="stack", facecam=Rect(x=900, y=400, w=380, h=320), gameplay=Rect(x=0, y=0, w=1280, h=720))]
    from stream_clipper.media import motion

    for i, lay in enumerate(layouts):
        dst = tmp_path / f"o{i}.mp4"
        zooms = [motion.Zoom(0.3, 0.2, 0.5, "chat", 99)]
        fx = motion.build_filter(zooms, out.motion, lay, out, 2.0)
        render(src, dst, start=0.5, duration=2.0, layout=lay, cfg=out, ass_path=ass, threads=2,
               fonts_dir=str(ROOT / "assets" / "fonts"), effects=fx)
        info = probe(dst)
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p" and info["acodec"] == "aac"
        assert 1.8 < info["duration"] < 2.3
    head = (tmp_path / "o0.mp4").read_bytes()[:200]
    assert head.find(b"moov") != -1 or head.find(b"ftyp") != -1  # faststart: moov near the start


@pytest.mark.ffmpeg
def test_shrink_to_size(tmp_path):
    from stream_clipper.media.render import shrink_to_size

    src = tmp_path / "big.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-b:v", "8M",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    limit = 2_000_000
    assert src.stat().st_size > limit
    out = shrink_to_size(src, tmp_path / "small.mp4", limit, duration=6, threads=2)
    assert out != src and out.stat().st_size <= limit
    assert shrink_to_size(out, tmp_path / "x.mp4", limit, duration=6) == out  # already small: untouched


def test_motion_moments_and_expression():
    import math

    import numpy as np

    from stream_clipper.config import MotionCfg
    from stream_clipper.media import motion

    cfg = MotionCfg()
    hop = 0.1
    db = np.full(600, -30.0)              # 60 s of normal talking
    db += np.random.default_rng(0).normal(0, 1.5, 600)
    for at in (12.0, 13.0, 30.0, 47.0):   # shouts; 12 and 13 are too close together
        i = int(at / hop)
        db[i:i + 8] = -8.0
    zooms = motion.pick_moments(db, hop, 60, cfg, chat_moments=[25.0])
    ts = [z.at for z in zooms]
    assert len(zooms) == 3 and zooms[[z.reason for z in zooms].index("chat")].at == 25.0
    assert all(b - a >= cfg.min_gap_seconds for a, b in zip(ts, ts[1:]))
    # flat audio: no audio zooms at all
    assert motion.pick_moments(np.full(600, -30.0), hop, 60, cfg) == []
    # zoom curve: 1 + ken burns at the start, ~1 + strength while holding
    T = "t"
    expr = motion.zoom_expr([motion.Zoom(10, 0.15, 1.6, "chat", 99)], cfg, 60, T)
    ev = lambda t: eval(expr.replace("t", str(t)), {"clip": lambda x, a, b: min(max(x, a), b)})  # noqa: S307
    assert math.isclose(ev(0), 1.0) and 1.14 < ev(11) < 1.17 and ev(20) < 1.04
    assert motion.build_filter([], MotionCfg(enabled=False), LayoutCfg(), OutputCfg(), 60) is None


def test_hook_title_in_ass():
    out = OutputCfg()
    ass = build_ass(_words("hello chat"), out, LayoutCfg(type="blur_fit"), hook="he actually did it", hook_seconds=3)
    hook = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    assert len(hook) == 1 and "HE ACTUALLY DID IT" in hook[0] and ",0:00:03.00,Hook," in hook[0]
    assert "Style: Hook," in ass
    assert "Dialogue: 1," not in build_ass(_words("hello chat"), out, LayoutCfg())

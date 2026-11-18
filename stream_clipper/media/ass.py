"""TikTok-style subtitles: word timestamps -> ASS file.

- 3-5 words per caption chunk (break early on pauses / sentence punctuation)
- at most 2 lines, each <= max_chars_per_line when possible
- optional active-word highlight (one event per word, current word coloured)
- vertical position chosen per layout so text never covers the facecam
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import LayoutCfg, OutputCfg

PAUSE_BREAK = 0.45  # seconds of silence that ends a chunk
SENTENCE_END = (".", "!", "?", "…")
SOFT_END = (",", ";", ":")


@dataclass
class Word:
    start: float
    end: float
    text: str


def subtitle_center_y(layout: LayoutCfg, height: int = 1920, width: int = 1080) -> int:
    """Vertical centre (px) of the subtitle block for a layout."""
    if layout.subtitle_y is not None:
        return round(layout.subtitle_y * height)
    if layout.type == "stack":
        # just below the facecam/gameplay seam, inside the gameplay area
        return layout.facecam_height + 170
    if layout.type == "blur_fit":
        # band below the centred 16:9 picture, above the bottom ~20 % platform UI
        fg_bottom = (height + width * 9 // 16) // 2
        return min(fg_bottom + 150, int(height * 0.78))
    return int(height * 0.62)  # center_crop


def chunk_words(words: list[Word], min_words: int = 3, max_words: int = 5, max_chars_line: int = 18) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur:
            gap = w.start - cur[-1].end
            last = cur[-1].text.rstrip()
            candidate = " ".join(x.text for x in cur + [w])
            if (len(cur) >= max_words
                    or gap >= PAUSE_BREAK
                    or last.endswith(SENTENCE_END)
                    or (last.endswith(SOFT_END) and len(cur) >= min_words)
                    or not _fits_two_lines(candidate, max_chars_line)):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _fits_two_lines(text: str, max_chars: int) -> bool:
    if len(text) <= max_chars:
        return True
    a, b = split_lines(text)
    return len(a) <= max_chars and len(b) <= max_chars


def split_lines(text: str) -> tuple[str, str]:
    """Split into two lines as evenly as possible at a word boundary."""
    words = text.split()
    if len(words) < 2:
        return text, ""
    best = min(range(1, len(words)), key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
    return " ".join(words[:best]), " ".join(words[best:])


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _hex_to_ass(color: str) -> str:
    r, g, b = color[1:3], color[3:5], color[5:7]
    return f"&H00{b}{g}{r}&".upper()


def _render_chunk(texts: list[str], max_chars: int, active: int | None, hl: str) -> str:
    joined = " ".join(texts)
    line1, line2 = (joined, "") if len(joined) <= max_chars else split_lines(joined)
    n1 = len(line1.split())
    parts = []
    for i, t in enumerate(texts):
        tok = _escape(t)
        if active is not None and i == active:
            tok = f"{{\\c{hl}}}{tok}{{\\c&H00FFFFFF&}}"
        sep = "" if i == 0 else ("\\N" if (line2 and i == n1) else " ")
        parts.append(sep + tok)
    return "".join(parts)


def hook_y(layout: LayoutCfg, height: int = 1920, width: int = 1080) -> int:
    """Centre of the hook title: the empty band above the picture (blur_fit), else the upper part."""
    if layout.type == "blur_fit":
        return max(140, (height - width * 9 // 16) // 4)
    return int(height * 0.09)


def build_ass(words: list[Word], out: OutputCfg, layout: LayoutCfg, *, font: str | None = None,
              hook: str | None = None, hook_seconds: float = 3.0) -> str:
    font = font or out.font
    y = subtitle_center_y(layout, out.height, out.width)
    x = out.width // 2
    hl = _hex_to_ass(out.highlight_color)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out.width}
PlayResY: {out.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{out.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Hook,{font},{round(out.font_size * 0.8)},&H00000000,&H00000000,{hl.rstrip('&')},&H00000000,0,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean = [Word(w.start, w.end, w.text.strip()) for w in words if w.text.strip()]
    if out.subtitle_uppercase:
        clean = [Word(w.start, w.end, w.text.upper()) for w in clean]
    chunks = chunk_words(clean, out.words_per_chunk_min, out.words_per_chunk_max, out.max_chars_per_line)
    lines = []
    pos = f"{{\\an5\\pos({x},{y})}}"
    for ci, ch in enumerate(chunks):
        start = ch[0].start
        end = ch[-1].end
        nxt = chunks[ci + 1][0].start if ci + 1 < len(chunks) else None
        if nxt is not None and nxt - end < 0.3:
            end = nxt  # avoid flicker between close chunks
        end = max(end, start + 0.3)
        texts = [w.text for w in ch]
        if not out.highlight_active_word:
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{pos}{_render_chunk(texts, out.max_chars_per_line, None, hl)}")
            continue
        for wi, w in enumerate(ch):
            ws = start if wi == 0 else w.start
            we = ch[wi + 1].start if wi + 1 < len(ch) else end
            if we <= ws:
                continue
            lines.append(f"Dialogue: 0,{_ts(ws)},{_ts(we)},Default,,0,0,0,,{pos}{_render_chunk(texts, out.max_chars_per_line, wi, hl)}")
    if hook and hook.strip() and hook_seconds > 0:
        text = _escape(hook.strip().upper())
        a, b = split_lines(text) if len(text) > 22 else (text, "")
        text = a + ("\\N" + b if b else "")
        hx, hy = out.width // 2, hook_y(layout, out.height, out.width)
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(hook_seconds)},Hook,,0,0,0,,"
                     f"{{\\an5\\pos({hx},{hy})\\fad(120,250)\\t(0,150,\\fscx108\\fscy108)\\t(150,300,\\fscx100\\fscy100)}}{text}")
    return header + "\n".join(lines) + "\n"


def words_in_range(segments: list[dict], start: float, end: float) -> list[Word]:
    """Pick transcript words inside [start, end] and shift them to clip-relative time."""
    out = []
    for seg in segments:
        for w in seg.get("words", []):
            if w["start"] >= start and w["end"] <= end:
                out.append(Word(w["start"] - start, w["end"] - start, w["word"]))
    return out

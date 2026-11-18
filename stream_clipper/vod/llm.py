"""Optional local LLM (Ollama) rating of candidate windows. Off by default."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass

import httpx

from .. import log
from ..config import LlmCfg

L = log.get("llm")

PROMPT = """You rate livestream transcript excerpts for short-form video (TikTok/Reels/Shorts).
Rate how likely this excerpt is to make a viral, self-contained short (0 = boring, 10 = must post).
Also write a short, punchy English hook title (max 8 words, no hashtags, no quotes).
Answer ONLY with JSON: {{"score": <0-10>, "hook": "<title>"}}

Creator: {creator}
Excerpt:
\"\"\"{text}\"\"\"
"""


def rate(cfg: LlmCfg, text: str, creator: str = "") -> tuple[float, str] | None:
    """(score 0-10, hook) or None if the LLM is unavailable/garbled (never fatal)."""
    if not cfg.enabled or not text.strip():
        return None
    try:
        r = httpx.post(f"{cfg.ollama_url.rstrip('/')}/api/generate", timeout=cfg.timeout_seconds, json={
            "model": cfg.model, "prompt": PROMPT.format(creator=creator, text=text[:4000]), "stream": False, "think": cfg.think,
            "format": "json", "options": {"temperature": 0.2, "num_predict": 80}})
        r.raise_for_status()
        d = json.loads(r.json().get("response", "{}"))
        score = float(d.get("score", 0))
        hook = str(d.get("hook", "")).strip().strip('"')[:80]
        return max(0.0, min(10.0, score)), hook
    except Exception as e:  # noqa: BLE001
        L.warning("llm rating failed", extra={"error": str(e)[:300]})
        return None


def hook(cfg: LlmCfg, text: str, creator: str = "") -> str | None:
    res = rate(cfg, text, creator)
    return res[1] if res and res[1] else None


# ------------------------------------------------------------------ live judge

JUDGE_PROMPT = """You are a picky short-form video editor. Chat spiked during a livestream; you decide if this moment
is worth posting on TikTok/Reels/Shorts and where to cut it. Chat spikes also happen for nothing (donations, polls,
emote spam), so not every moment is worth posting. You only see the speech, not the video: what the streamer SAYS
and how chat reacts is your evidence.

Creator: {creator}
Chat spiked at second: {moments} (the thing they reacted to happened just before)
Most repeated chat messages: {chat}
Speech transcript ({total:.0f} s of footage, timestamps in seconds):
{transcript}

Answer these about the clip as a stranger scrolling TikTok would see it:
 event: does something specific happen? A play, fail, win, loss, scare or prank counts, and so does a moment in the
        talking: a rant, roast, argument, hot take, confession, wild story, funny line. Plain small talk, reading
        donations, waiting or explaining game mechanics does not count.
 payoff: is there a clear punchline, result or big reaction you can point to?
 standalone: would a stranger understand it without knowing the stream?
 intensity: 1 calm ... 5 screaming / losing it / everyone in shock
 score: overall 0-10 (0-2 nothing, 3-4 small or needs context, 5 decent, 6-7 clearly good, 8-10 viral)
Chat messages that are only emotes or "W"/"L" spam do not prove anything happened, but laughing (LUL, KEKW, LMAO)
right after a line means the line landed. With almost no speech judge from what the chat messages describe.

Cut: {min_len:.0f}-{max_len:.0f} s long, must contain second {first_moment}, start 10-25 s before it at the
beginning of the situation (not mid-sentence), end shortly after the reaction settles.

Hook: on-screen title for the first 3 seconds that makes a stranger stay. Max 6 words, English, specific to THIS
clip (names the thing that happens, teases the payoff without spoiling it), never about chat, no generic phrases,
no hashtags, no emojis, no quotes.

Answer ONLY with JSON:
{{"what_happens": "<one sentence>", "event": <true|false>, "payoff": <true|false>, "standalone": <true|false>, "intensity": <1-5>, "score": <0-10>, "start": <second>, "end": <second>, "hook": "<title>", "reason": "<max 12 words>"}}
"""


_GENERIC_HOOK = re.compile(r"\b(chat|viewers?|reacts?|reactions?)\b", re.I)


def clean_hook(h) -> str:
    """Hooks about the chat / 'creator reacts' are worse than none: drop them."""
    h = re.sub(r"[#\"“”]", "", str(h or "")).strip()[:60]
    return "" if (not h or _GENERIC_HOOK.search(h)) else h


def _yes(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def combined_score(d: dict) -> float:
    """Checklist (small models answer yes/no far more reliably than they calibrate 0-10) blended with the raw score.
    Nothing specific happens -> capped low, whatever the model's overall score says."""
    raw = max(0.0, min(10.0, float(d.get("score", 0))))
    event, payoff, alone = _yes(d.get("event")), _yes(d.get("payoff")), _yes(d.get("standalone"))
    try:
        inten = max(1.0, min(5.0, float(d.get("intensity", 1))))
    except (TypeError, ValueError):
        inten = 1.0
    check = 3.0 * event + 2.5 * payoff + 1.5 * alone + 0.75 * (inten - 1)   # 0..10
    s = 0.6 * check + 0.4 * raw
    if not event:
        s = min(s, 5.0)
    return round(s, 1)


@dataclass
class Verdict:
    score: float
    start: float
    end: float
    hook: str
    reason: str
    trimmed: bool


def _fmt_transcript(segments: list[dict], limit: int = 6000) -> str:
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text'].strip()}" for s in segments if s.get("text", "").strip()]
    out = "\n".join(lines) or "(no speech)"
    return out[:limit]


def fit_window(start: float, end: float, *, total: float, min_len: float, max_len: float,
               moments: list[float], default: tuple[float, float], min_lead: float = 10.0) -> tuple[float, float, bool]:
    """Make the LLM's cut legal: inside the footage, min..max long, chat moment inside with at least `min_lead`
    seconds of setup before it. Else the default window."""
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return default[0], default[1], False
    if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
        return default[0], default[1], False
    if moments and start > moments[0] - min_lead:  # no setup: the viewer would miss what the reaction is about
        shift = start - max(0.0, moments[0] - min_lead)
        start, end = start - shift, end - shift
    start = max(0.0, start)
    end = min(total, end)
    length = end - start
    if length < min_len:                       # too short: grow the tail first (keeps the setup), then the head
        end = min(total, start + min_len)
        start = max(0.0, end - min_len)
    elif length > max_len:                     # too long: keep the part around the chat moment
        m = moments[0] if moments else start
        start = max(start, min(m - 8.0, end - max_len))
        end = start + max_len
    if end - start < min_len - 0.5:
        return default[0], default[1], False
    if moments and not any(start + 1.0 <= m <= end - 2.0 for m in moments):
        return default[0], default[1], False
    return round(start, 2), round(end, 2), True


def judge_live(cfg: LlmCfg, segments: list[dict], *, creator: str, moments: list[float], chat: list[dict],
               total: float, min_len: float, max_len: float, default: tuple[float, float]) -> Verdict | None:
    """Rate a live clip and pick its cut from the timestamped transcript. None if the LLM is unavailable."""
    if not cfg.enabled:
        return None
    chat_s = "; ".join(f"{c['text']} (x{c['count']})" for c in chat[:8]) or "(none)"
    prompt = JUDGE_PROMPT.format(creator=creator or "unknown", moments=", ".join(f"{m:.0f}" for m in moments) or "?",
                                 first_moment=f"{moments[0]:.0f}" if moments else "?",
                                 chat=chat_s[:600], total=total, transcript=_fmt_transcript(segments),
                                 min_len=min_len, max_len=max_len)
    try:
        r = httpx.post(f"{cfg.ollama_url.rstrip('/')}/api/generate", timeout=cfg.timeout_seconds, json={
            "model": cfg.model, "prompt": prompt, "stream": False, "think": cfg.think, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_predict": 300, "num_ctx": 4096}})
        r.raise_for_status()
        d = json.loads(r.json().get("response", "{}"))
        score = combined_score(d)
    except Exception as e:  # noqa: BLE001
        L.warning("llm judge failed", extra={"error": str(e)[:300]})
        return None
    if cfg.trim:
        start, end, ok = fit_window(d.get("start"), d.get("end"), total=total, min_len=min_len, max_len=max_len,
                                    moments=moments, default=default)
    else:
        (start, end), ok = default, False
    hook = clean_hook(d.get("hook", ""))
    reason = str(d.get("reason", "") or d.get("what_happens", "")).strip()[:160]
    return Verdict(score, start, end, hook, reason, ok)


def ensure_model(cfg: LlmCfg, wait_seconds: float = 600) -> bool:
    """Pull the model into Ollama if it is missing (first start). Never fatal."""
    if not cfg.enabled:
        return False
    base = cfg.ollama_url.rstrip("/")
    deadline = time.time() + wait_seconds
    while True:
        try:
            tags = httpx.get(f"{base}/api/tags", timeout=10).json().get("models", [])
            break
        except Exception:  # noqa: BLE001
            if time.time() > deadline:
                L.warning("ollama not reachable; live clips are rendered without LLM", extra={"url": base})
                return False
            time.sleep(5)
    names = {t.get("name") for t in tags} | {t.get("model") for t in tags}
    if cfg.model in names or f"{cfg.model}:latest" in names:
        return True
    L.info("pulling llm model (one-time, a few GB)", extra={"model": cfg.model})
    try:
        r = httpx.post(f"{base}/api/pull", json={"model": cfg.model, "stream": False}, timeout=3600)
        r.raise_for_status()
        L.info("llm model ready", extra={"model": cfg.model})
        return True
    except Exception as e:  # noqa: BLE001
        L.warning("llm model pull failed", extra={"error": str(e)[:300]})
        return False

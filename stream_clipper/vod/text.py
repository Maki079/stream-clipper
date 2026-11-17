"""Transcript features on a 1-second grid + quotable line picking."""
from __future__ import annotations

import re

import numpy as np


def speech_mask(segments: list[dict], n: int) -> np.ndarray:
    m = np.zeros(n, dtype=np.float32)
    for s in segments:
        for w in s.get("words", []):
            a, b = int(w["start"]), int(np.ceil(w["end"]))
            m[max(0, a):min(n, b)] = 1
    return m


def word_excitement(word: str) -> float:
    score = 0.0
    if "!" in word:
        score += 1.0
    if "?" in word:
        score += 0.5
    letters = re.sub(r"[^A-Za-z]", "", word)
    if len(letters) >= 3 and letters.isupper():
        score += 0.5
    return score


def features(segments: list[dict], n: int, keywords: list[str]) -> dict[str, np.ndarray]:
    density = np.zeros(n, dtype=np.float32)
    excite = np.zeros(n, dtype=np.float32)
    kws = [k.lower() for k in keywords]
    for s in segments:
        for w in s.get("words", []):
            i = int(w["start"])
            if 0 <= i < n:
                density[i] += 1
                excite[i] += word_excitement(w["word"])
        text = s.get("text", "").lower()
        hits = sum(text.count(k) for k in kws)
        if hits:
            a, b = int(s["start"]), int(np.ceil(s["end"]))
            excite[max(0, a):min(n, b)] += hits / max(1, b - a) * 2
    # words/sec smoothed over 5 s
    density = np.convolve(density, np.ones(5) / 5, mode="same").astype(np.float32)
    return {"speech_density": density, "excitement": excite}


def sentences(segments: list[dict], start: float, end: float) -> list[str]:
    out = []
    for s in segments:
        if s["start"] >= start - 0.5 and s["end"] <= end + 0.5:
            out += [x.strip() for x in re.split(r"(?<=[.!?])\s+", s["text"]) if x.strip()]
    return out


def quotable_line(segments: list[dict], start: float, end: float, keywords: list[str] = ()) -> str | None:
    """Short, punchy sentence from the clip: 4-14 words, prefers ! / ? / keywords."""
    best, best_score = None, 0.0
    kws = [k.lower() for k in keywords]
    for sent in sentences(segments, start, end):
        words = sent.split()
        if not 4 <= len(words) <= 14:
            continue
        score = 1.0 + sum(word_excitement(w) for w in words) + sum(sent.lower().count(k) for k in kws)
        score -= abs(len(words) - 8) * 0.1
        if score > best_score:
            best, best_score = sent, score
    return best

"""Combine per-second features into a highlight score and pick the best clip windows."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Window:
    start: float
    end: float
    score: float
    hook: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def length(self) -> float:
        return self.end - self.start


def robust_z(v: np.ndarray) -> np.ndarray:
    if len(v) == 0:
        return v
    med = np.median(v)
    mad = np.median(np.abs(v - med)) * 1.4826
    if mad < 1e-6:
        sd = v.std()
        mad = sd if sd > 1e-6 else 1.0
    return np.clip((v - med) / mad, -2, 6)


def combine(feats: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    n = min(len(v) for v in feats.values()) if feats else 0
    total = np.zeros(n, dtype=np.float32)
    wsum = 0.0
    for name, v in feats.items():
        w = weights.get(name, 0.0)
        if w <= 0 or n == 0:
            continue
        total += w * robust_z(v[:n].astype(np.float32))
        wsum += w
    return total / wsum if wsum else total


def select_top(score: np.ndarray, min_len: int, max_len: int, n: int, *, gap: float = 5.0, step: int = 5) -> list[Window]:
    """Greedy: repeatedly take the best-scoring window (mean score, any length in min..max by `step`)
    that doesn't overlap (incl. `gap`) any window already taken."""
    total = len(score)
    cs = np.concatenate([[0.0], np.cumsum(score, dtype=np.float64)])
    lengths = sorted(set(list(range(min_len, max_len + 1, step)) + [max_len]))
    means = {L: (cs[L:] - cs[:-L]) / L for L in lengths if L <= total}
    picked: list[Window] = []
    while len(picked) < n and means:
        best: Window | None = None
        for L, m in means.items():
            if not len(m):
                continue
            i = int(np.argmax(m))
            if m[i] == -np.inf:
                continue
            if best is None or m[i] > best.score:
                best = Window(float(i), float(i + L), float(m[i]))
        if best is None:
            break
        picked.append(best)
        for L, m in means.items():  # forbid starts that would overlap the new window (+gap)
            lo = max(0, int(np.floor(best.start - gap - L)) + 1)
            hi = min(len(m), int(np.ceil(best.end + gap)))
            if lo < hi:
                m[lo:hi] = -np.inf
    return picked


def snap(w: Window, segments: list[dict], min_len: float, max_len: float, tol: float = 6.0, duration: float | None = None) -> Window:
    """Move start/end to the nearest transcript segment (sentence) boundaries within tol seconds,
    as long as the length stays within [min_len, max_len]."""
    starts = [s["start"] for s in segments if abs(s["start"] - w.start) <= tol]
    ends = [s["end"] for s in segments if abs(s["end"] - w.end) <= tol]
    best = (w.start, w.end)
    best_cost = float("inf")
    for a in starts or [w.start]:
        for b in ends or [w.end]:
            L = b - a
            if min_len <= L <= max_len and (duration is None or b <= duration + 0.01) and a >= 0:
                cost = abs(a - w.start) + abs(b - w.end)
                if cost < best_cost:
                    best, best_cost = (a, b), cost
    return Window(round(best[0], 2), round(best[1], 2), w.score, w.hook, w.detail)

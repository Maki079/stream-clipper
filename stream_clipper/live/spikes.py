"""Chat spike detection (pure logic, no I/O; driven by add() + tick(now)).

Every tick (1 s) we measure a sliding window (default 10 s): message count and unique
chatters. The baseline is the mean/std of those measurements over the last 5 minutes,
excluding samples taken during/just after a spike. The average is recomputed every tick,
so the threshold follows the chat as it grows or calms down:

    threshold = max(min_messages, multiplier * avg, avg + z_threshold * std)
    spike     = messages_in_window >= threshold     (same formula for unique chatters)

It ends when the condition has been false for one window length (or after max_seconds).
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from ..config import SpikeCfg


@dataclass
class ChatMessage:
    ts: float          # our receive time (epoch seconds)
    user: str
    text: str


@dataclass
class Spike:
    start: float               # onset (first triggering tick)
    end: float                 # last triggering tick
    peak_messages: int
    peak_unique: int
    peak_z: float
    peak_ratio: float
    baseline_messages: float
    top_messages: list[tuple[str, int]] = field(default_factory=list)

    @property
    def strength(self) -> float:
        """Ranking score: mostly z-score, bounded so huge chats don't dwarf everything."""
        return round(min(self.peak_z, 50.0) + min(self.peak_ratio, 20.0), 2)


@dataclass
class _Sample:
    ts: float
    messages: int
    unique: int
    in_spike: bool


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


class SpikeDetector:
    def __init__(self, cfg: SpikeCfg, started_at: float, max_spike_seconds: float = 120):
        self.cfg = cfg
        self.started_at = started_at
        self.max_spike = max_spike_seconds
        self._patterns = [re.compile(p) for p in cfg.ignore_patterns]
        self._ignore_users = {u.lower() for u in cfg.ignore_users}
        self._win: deque[tuple[float, str, str]] = deque()  # (ts, user, normalized text)
        self._hist: deque[_Sample] = deque()
        self._active: Spike | None = None
        self._quiet_since: float | None = None
        self._exclude_until = 0.0
        self.ignored = 0
        self.last: dict = {}

    # ---------------------------------------------------------------- input
    def accepts(self, m: ChatMessage) -> bool:
        if m.user.lower() in self._ignore_users:
            return False
        return not any(p.search(m.text) for p in self._patterns)

    def add(self, m: ChatMessage) -> None:
        if not self.accepts(m):
            self.ignored += 1
            return
        self._win.append((m.ts, m.user.lower(), normalize(m.text)))

    def reset(self, now: float) -> None:
        """Stream (re)started: forget everything, warm up again."""
        self.__init__(self.cfg, now, self.max_spike)

    # ------------------------------------------------------------- measures
    def _measure(self, now: float) -> tuple[int, int, list[tuple[str, int]]]:
        lo = now - self.cfg.window_seconds
        while self._win and self._win[0][0] < lo:
            self._win.popleft()
        seen: set[tuple[str, str]] = set()
        users: set[str] = set()
        texts: Counter[str] = Counter()
        for ts, user, text in self._win:
            if ts > now:
                continue
            if (user, text) in seen:  # same user repeating the same line counts once
                continue
            seen.add((user, text))
            users.add(user)
            texts[text] += 1
        return len(seen), len(users), texts.most_common(5)

    def baseline(self, now: float) -> tuple[float, float, float, float]:
        """(mean_msgs, std_msgs, mean_unique, std_unique) over clean history."""
        lo = now - self.cfg.baseline_seconds
        while self._hist and self._hist[0].ts < lo:
            self._hist.popleft()
        clean = [s for s in self._hist if not s.in_spike]
        if not clean:
            return 0.0, 1.0, 0.0, 1.0
        return (*_mean_std([s.messages for s in clean]), *_mean_std([s.unique for s in clean]))

    def threshold(self, mean: float, std: float, floor: float) -> float:
        return max(floor, self.cfg.multiplier * mean, mean + self.cfg.z_threshold * std)

    def _triggered(self, v: float, mean: float, std: float, floor: float) -> tuple[bool, float, float]:
        z = (v - mean) / std
        ratio = v / mean if mean > 0 else (math.inf if v > 0 else 0.0)
        return v >= self.threshold(mean, std, floor), z, ratio

    # ----------------------------------------------------------------- tick
    def tick(self, now: float) -> Spike | None:
        """Advance to `now`. Returns a Spike when one has just finished."""
        msgs, uniq, top = self._measure(now)
        mm, sm, mu, su = self.baseline(now)
        warm = now - self.started_at >= self.cfg.warmup_seconds
        t_m, z_m, r_m = self._triggered(msgs, mm, sm, self.cfg.min_messages)
        t_u, _, _ = self._triggered(uniq, mu, su, self.cfg.min_unique_chatters)
        rel = {"messages": t_m, "unique": t_u, "either": t_m or t_u, "both": t_m and t_u}[self.cfg.metric]
        absolute = msgs >= self.cfg.min_messages and uniq >= self.cfg.min_unique_chatters
        firing = warm and rel and absolute
        self.last = {"messages": msgs, "unique": uniq, "avg": round(mm, 1), "std": round(sm, 1),
                     "threshold": round(self.threshold(mm, sm, self.cfg.min_messages), 1), "warm": warm}

        in_spike = firing or self._active is not None or now < self._exclude_until
        self._hist.append(_Sample(now, msgs, uniq, in_spike))

        finished: Spike | None = None
        if firing:
            if self._active is None:
                self._active = Spike(now, now, msgs, uniq, z_m, r_m if math.isfinite(r_m) else 99.0, mm, top)
            a = self._active
            a.end = now
            if msgs > a.peak_messages:
                a.peak_messages, a.top_messages = msgs, top
            a.peak_unique = max(a.peak_unique, uniq)
            a.peak_z = max(a.peak_z, z_m)
            a.peak_ratio = max(a.peak_ratio, r_m if math.isfinite(r_m) else 99.0)
            self._quiet_since = None
        elif self._active is not None and self._quiet_since is None:
            self._quiet_since = now

        if self._active is not None:
            quiet_long = self._quiet_since is not None and now - self._quiet_since >= self.cfg.window_seconds
            too_long = now - self._active.start >= self.max_spike
            if quiet_long or too_long:
                finished, self._active, self._quiet_since = self._active, None, None
                # the sliding window still contains spike messages for a while: keep them out of the baseline
                self._exclude_until = now + self.cfg.window_seconds
        return finished

    def flush(self) -> Spike | None:
        a, self._active = self._active, None
        return a


def _mean_std(vals: list[int]) -> tuple[float, float]:
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    # floor: Poisson-ish noise, never below 1 -> quiet chats don't explode the z-score
    return mean, max(math.sqrt(var), math.sqrt(mean), 1.0)

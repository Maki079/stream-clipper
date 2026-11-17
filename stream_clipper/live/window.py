"""Clip window math and spike merging (pure logic).

Wall-clock alignment: video segments and chat messages are both stamped with OUR clock
when received. Chatters react `chat_delay_seconds` after they see the moment, so the
moment in the video is approximately `spike_onset - chat_delay_seconds`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CampaignCfg, ClipCfg
from .spikes import Spike


def length_limits(clip: ClipCfg, campaigns: list[CampaignCfg]) -> tuple[float, float]:
    """(min_len, max_len): longest required minimum of all tagged campaigns, capped by max."""
    min_len = max((c.min_seconds for c in campaigns), default=0)
    max_len = min([clip.max_seconds] + [c.max_seconds for c in campaigns])
    if min_len > max_len:  # conflicting rules: satisfy the minimum, it's what disqualifies clips
        max_len = min_len
    return float(min_len), float(max_len)


def clip_window(onset: float, spike_end: float, clip: ClipCfg, min_len: float, max_len: float) -> tuple[float, float]:
    moment = onset - clip.chat_delay_seconds
    start = moment - clip.pre_seconds
    end = max(moment + clip.post_seconds, spike_end - clip.chat_delay_seconds + 10)
    if end - start < min_len:
        end = start + min_len           # extend the tail, keep the build-up
    if end - start > max_len:
        end = start + max_len
    return start, end


@dataclass
class PlannedClip:
    start: float
    end: float
    spikes: list[Spike] = field(default_factory=list)

    @property
    def score(self) -> float:
        return max(s.strength for s in self.spikes)


class ClipPlanner:
    """Turns spikes into clip windows; merges nearby spikes; applies cooldown.

    moment = spike onset - chat_delay. Rules for each new spike:
      1. moment inside the open clip -> merge (extend the end, capped at max_len)
      2. moment within `cooldown` of the previous accepted spike -> merge into the open clip if
         the result stays <= max_len, otherwise drop
      3. otherwise -> close the open clip and start a new one (never overlapping the previous)
    A clip is ready once wall clock passes its end + margin (ready()).
    """

    def __init__(self, clip: ClipCfg, min_len: float, max_len: float, cooldown: float, margin: float = 8.0):
        self.clip, self.min_len, self.max_len, self.cooldown, self.margin = clip, min_len, max_len, cooldown, margin
        self.open: PlannedClip | None = None
        self.last_end: float = float("-inf")
        self.last_moment: float = float("-inf")
        self._closed: list[PlannedClip] = []

    def on_spike(self, s: Spike) -> tuple[str, PlannedClip | None]:
        moment = s.start - self.clip.chat_delay_seconds
        start, end = clip_window(s.start, s.end, self.clip, self.min_len, self.max_len)
        o = self.open
        if o is not None and moment <= o.end:
            o.end = min(max(o.end, end), o.start + self.max_len)
            o.spikes.append(s)
            self.last_moment = moment
            return "extended", o
        if moment < self.last_moment + self.cooldown:
            if o is not None and max(o.end, end) - o.start <= self.max_len:
                o.end = max(o.end, end)
                o.spikes.append(s)
                self.last_moment = moment
                return "extended", o
            return "dropped", None
        if o is not None:
            self._close(o)
        new = PlannedClip(max(start, self.last_end), end, [s])
        if new.end - new.start < self.min_len:
            new.end = new.start + self.min_len
        self.open = new
        self.last_moment = moment
        return "new", new

    def _close(self, c: PlannedClip) -> None:
        self._closed.append(c)
        self.last_end = max(self.last_end, c.end)
        if self.open is c:
            self.open = None

    def ready(self, now: float) -> list[PlannedClip]:
        if self.open is not None and now >= self.open.end + self.margin:
            self._close(self.open)
        out, self._closed = self._closed, []
        return out

    def flush(self) -> list[PlannedClip]:
        if self.open is not None:
            self._close(self.open)
        out, self._closed = self._closed, []
        return out

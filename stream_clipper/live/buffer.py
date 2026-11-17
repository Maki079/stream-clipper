"""Rolling segment buffer on disk.

The recorder's ffmpeg writes <session>_<seq>.ts (sequence numbers, never collide) and appends
one line per *finished* segment to <session>.csv: "file,start,end" in stream (content) seconds.
Content time is mapped to wall-clock time with a per-session anchor:

    wall = anchor + content_time,   anchor = median(mtime(file) - end) over the session's segments

(the first two segments are skipped: they arrive in a burst from the HLS back-buffer). Once
enough segments exist the anchor is frozen in <session>.anchor so times stay stable.
Segment lengths come from the content, not from arrival times, so bursty delivery can't
create gaps or overlaps.

Legacy files named <session>_<epoch>.ts (old recorder, no csv) still work: start = epoch,
end = mtime.
"""
from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

ANCHOR_SAMPLES = 8


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float
    session: str


def _read_csv(p: Path) -> list[tuple[str, float, float]]:
    rows = []
    try:
        text = p.read_text()
    except FileNotFoundError:
        return rows
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue  # partially written line
        try:
            rows.append((parts[0], float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return rows


def _anchor(d: Path, session: str, rows: list[tuple[str, float, float]], mtimes: dict[str, float]) -> float | None:
    af = d / f"{session}.anchor"
    try:
        return float(af.read_text())
    except (FileNotFoundError, ValueError):
        pass
    samples = [mtimes[f] - end for f, _, end in rows[2:] if f in mtimes] or \
              [mtimes[f] - end for f, _, end in rows if f in mtimes]
    if not samples:
        return None
    a = statistics.median(samples)
    if len(samples) >= ANCHOR_SAMPLES:
        tmp = af.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(f"{a:.3f}")
        tmp.replace(af)
    return a


def list_segments(d: Path, *, now: float | None = None, settle: float = 1.0) -> list[Segment]:
    """Finished segments, oldest first."""
    segs: list[Segment] = []
    mtimes: dict[str, float] = {}
    by_session: dict[str, list[Path]] = {}
    for p in d.glob("*.ts"):
        try:
            mtimes[p.name] = p.stat().st_mtime
        except FileNotFoundError:
            continue
        by_session.setdefault(p.stem.rsplit("_", 1)[0], []).append(p)
    legacy: list[Segment] = []
    for session, files in by_session.items():
        rows = _read_csv(d / f"{session}.csv")
        if rows:
            anchor = _anchor(d, session, rows, mtimes)
            if anchor is None:
                continue
            segs += [Segment(d / f, anchor + s, anchor + e, session) for f, s, e in rows if f in mtimes]
            continue
        for p in files:  # legacy epoch-named files (new-format names are 6-digit sequence numbers)
            suffix = p.stem.rsplit("_", 1)[-1]
            if len(suffix) == 6 or not suffix.isdigit():
                continue  # new format, csv not written yet -> not finished
            legacy.append(Segment(p, float(suffix), mtimes[p.name], session))
    legacy.sort(key=lambda s: (s.start, s.path.name))
    now = time.time() if now is None else now
    if legacy and now - legacy[-1].end < settle:
        legacy = legacy[:-1]  # still being written
    segs += legacy
    segs.sort(key=lambda s: (s.start, s.path.name))
    return segs


def select(segs: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segs if s.end > start and s.start < end]


def coverage(chosen: list[Segment], start: float, end: float, max_gap: float = 3.0) -> tuple[bool, list[tuple[float, float]]]:
    """(complete, gaps). Complete means [start, end] is covered without gaps > max_gap."""
    gaps = []
    t = start
    for s in chosen:
        if s.start - t > max_gap:
            gaps.append((t, s.start))
        t = max(t, s.end)
    if end - t > max_gap:
        gaps.append((t, end))
    return not gaps, gaps


def prune(d: Path, retention: float, pinned_from: float | None, *, now: float | None = None,
          keep_session: str | None = None) -> int:
    now = time.time() if now is None else now
    limit = now - retention
    if pinned_from is not None:
        limit = min(limit, pinned_from - 10)
    n = 0
    for s in list_segments(d, now=now):
        if s.end < limit:
            s.path.unlink(missing_ok=True)
            n += 1
    # drop bookkeeping files of sessions that have no video left
    live_sessions = {p.stem.rsplit("_", 1)[0] for p in d.glob("*.ts")}
    for ext in ("csv", "anchor"):
        for f in d.glob(f"*.{ext}"):
            if f.stem not in live_sessions and f.stem != keep_session:
                f.unlink(missing_ok=True)
    return n

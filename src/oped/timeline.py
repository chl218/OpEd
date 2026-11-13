"""Timeline math: cut merging, keep intervals, and source->output time mapping."""
from __future__ import annotations

Interval = tuple[float, float]

MERGE_GAP = 0.001
MIN_KEEP = 0.05


def merge_cuts(cuts: list[Interval], duration: float) -> list[Interval]:
    """Clamp to [0, duration], sort, merge overlapping/touching, drop empty."""
    clamped = []
    for s, e in cuts:
        s, e = max(0.0, float(s)), min(float(duration), float(e))
        if e > s:
            clamped.append((s, e))
    clamped.sort()
    merged: list[Interval] = []
    for s, e in clamped:
        if merged and s - merged[-1][1] < MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _complement(merged: list[Interval], lo: float, hi: float) -> list[Interval]:
    """Pieces of [lo, hi] outside the (merged) cuts; never zero-length."""
    out: list[Interval] = []
    pos = lo
    for s, e in merged:
        if e <= pos:
            continue
        if s >= hi:
            break
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < hi:
        out.append((pos, hi))
    return out


def keep_intervals(cuts: list[Interval], duration: float) -> list[Interval]:
    merged = merge_cuts(cuts, duration)
    return [(s, e) for s, e in _complement(merged, 0.0, float(duration)) if e - s >= MIN_KEEP]


class TimeMap:
    def __init__(self, cuts: list[Interval], duration: float):
        self.duration = float(duration)
        self.cuts = merge_cuts(cuts, duration)
        self.out_duration = self.duration - sum(e - s for s, e in self.cuts)

    def _removed(self, t: float) -> float:
        total = 0.0
        for s, e in self.cuts:
            if s >= t:
                break
            total += min(e, t) - s
        return total

    def map(self, t: float) -> float | None:
        for s, e in self.cuts:
            if s < t < e:
                return None
        return t - self._removed(t)

    def snap(self, t: float) -> float:
        return t - self._removed(t)

    def map_span(self, a: float, b: float) -> Interval | None:
        pieces = _complement(self.cuts, a, b) if b > a else []
        if not pieces:
            return None
        return (self.snap(pieces[0][0]), self.snap(pieces[-1][1]))

    def map_span_whole(self, a: float, b: float) -> Interval | None:
        if b <= a:
            return None
        pieces = _complement(self.cuts, a, b)
        kept = sum(e - s for s, e in pieces)
        if not pieces or kept / (b - a) < 0.5 - 1e-9:
            return None
        start = self.snap(pieces[0][0])
        return (start, min(start + (b - a), self.out_duration))

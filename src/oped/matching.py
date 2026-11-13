"""Locate a reference (OP/ED chroma features) inside an episode's chroma features.

Algorithm (all arrays are ``(frames, 12)`` chroma rows from ``fingerprint``):

1. ``score(t) = mean_i dot(ref[i], ep[t+i])`` for every offset ``t``, computed with one
   FFT cross-correlation summed over the 12 bins. Offsets may run past either end of the
   episode; the sum is normalised by the overlap length and only offsets whose overlap
   is at least 50 % of ``len(ref)`` are considered.
2. ``t* = argmax score``.
3. Per-frame similarity ``s[i] = dot(ref[i], ep[t*+i])`` over the overlapping frames,
   smoothed with a centred moving average of ``SMOOTH_FRAMES`` frames. Near the ends of
   the overlap the average is taken over the frames that exist (no zero padding), so a
   match that touches the start/end of the file or of the reference is not shortened.
4. The matched span is the longest run with smoothed ``s >= THRESHOLD``.
5. ``confidence = mean(s over span) * min(1, span_len / (0.5 * len(ref)))``.
6. ``start = (t* + span_first) / fps``, ``end = (t* + span_last + 1) / fps``.

Tuned values: ``THRESHOLD = 0.6`` and ``SMOOTH_FRAMES = 20`` (2 s at 10 fps), i.e. the
values from the plan, unchanged.

``find_reference`` returns ``None`` when ``len(ref) == 0``, when
``len(ep) < len(ref) // 2``, or when no frame of the best alignment reaches the threshold.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from oped.fingerprint import FPS

THRESHOLD = 0.6
SMOOTH_FRAMES = 20
MIN_OVERLAP = 0.5


@dataclass
class Match:
    start: float
    end: float
    confidence: float


def _cross_correlation(ref: np.ndarray, ep: np.ndarray) -> np.ndarray:
    """c[k] = sum_i dot(ref[i], ep[k - (m-1) + i]) for k in 0 .. n+m-2."""
    m, n = len(ref), len(ep)
    size = n + m - 1
    nfft = _next_fast_len(size)
    fe = np.fft.rfft(ep, nfft, axis=0)
    fr = np.fft.rfft(ref[::-1], nfft, axis=0)
    return np.fft.irfft((fe * fr).sum(axis=1), nfft)[:size]


def _next_fast_len(n: int) -> int:
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return best


def _longest_run(mask: np.ndarray) -> tuple[int, int] | None:
    """(first, last) indices of the longest run of True, or None."""
    if not mask.any():
        return None
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)  # exclusive
    k = int(np.argmax(ends - starts))
    return int(starts[k]), int(ends[k] - 1)


def _moving_average(s: np.ndarray, w: int) -> np.ndarray:
    k = np.ones(w)
    num = np.convolve(s, k, mode="same")
    den = np.convolve(np.ones(len(s)), k, mode="same")
    return num / den


def find_reference(ref: np.ndarray, ep: np.ndarray, fps: float = FPS) -> Match | None:
    ref = np.asarray(ref, dtype=np.float64)
    ep = np.asarray(ep, dtype=np.float64)
    m, n = len(ref), len(ep)
    if m == 0 or n < m // 2 or n == 0:
        return None

    corr = _cross_correlation(ref, ep)
    offsets = np.arange(-(m - 1), n)
    overlap = np.minimum(m, n - offsets) - np.maximum(0, -offsets)
    ok = overlap >= MIN_OVERLAP * m
    if not ok.any():
        return None
    score = np.where(ok, corr / np.maximum(overlap, 1), -np.inf)
    t = int(offsets[int(np.argmax(score))])

    lo, hi = max(0, -t), min(m, n - t)  # ref frames that overlap the episode
    s = (ref[lo:hi] * ep[t + lo:t + hi]).sum(axis=1)
    smooth = _moving_average(s, min(SMOOTH_FRAMES, len(s)))
    run = _longest_run(smooth >= THRESHOLD)
    if run is None:
        return None
    first, last = run
    span_len = last - first + 1
    conf = float(s[first:last + 1].mean()) * min(1.0, span_len / (0.5 * m))
    return Match(start=(t + lo + first) / fps, end=(t + lo + last + 1) / fps,
                 confidence=conf)

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

``discover_references`` finds OP/ED candidates with no hints:

1. Features are averaged in blocks of ``round(fps / 2)`` frames (2 fps) and re-normalised.
2. For each consecutive pair (A, B) in the dict's order, the OP regions (first 40 %) and
   the ED regions (last 25 %) are compared separately. The block similarity matrix is
   skewed so each diagonal is one row; gaps of up to 2 blocks are closed and the longest
   run with similarity >= 0.7 over all diagonals is found in one vectorised pass.
3. A run of >= ``min_len`` is refined to full-fps edges, located in all episodes with
   ``find_reference``, and kept when ``support`` (matches with confidence >= WEAK) is
   >= ``max(2, ceil(n / 2))``. Candidates of one kind that match each other at >= HIGH are
   deduplicated, keeping the higher support (then the longer). Sorted by (kind, start).

Changes from the plan's values: thresholds (0.7, 2-block gap, 30 s) are unchanged. Two
additions were needed for the edge accuracy the tests require (+-1 s):

* Block phases: B's blocks are taken at every phase (0..block-1 frames) and the
  similarity is the max over phases, so an OP offset that is not a whole 0.5 s block
  still lines up (with 0.25 s notes a half-block shift drops the similarity below 0.7).
* Edge refinement: a coarse run overshoots by 1-2 s (chance similarity of the background
  next to the music, plus gap closing). The run, padded by 1 s, is located in B with
  ``find_reference``, and that B span is located back in A; ``find_reference``'s
  threshold trims the non-matching edge frames. Candidate times are A's (full fps).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from oped.fingerprint import FPS
from oped.project import HIGH, WEAK, Kind

THRESHOLD = 0.6
SMOOTH_FRAMES = 20
MIN_OVERLAP = 0.5

DISC_FPS = 2.0      # discovery works on 2 fps blocks (5 frames at 10 fps)
DISC_SIM = 0.7      # per-block similarity for a diagonal run
DISC_GAP = 2        # blocks below DISC_SIM tolerated inside a run
OP_REGION = 0.40    # first 40 % of a file
ED_REGION = 0.25    # last 25 % of a file
REFINE_PAD = 1.0    # seconds added around a coarse run before refining its edges


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


# ---------------------------------------------------------------- discovery

@dataclass
class Candidate:
    kind: Kind
    episode: str
    start: float
    end: float
    support: int


def _downsample(f: np.ndarray, block: int, phase: int = 0) -> np.ndarray:
    """Average blocks of ``block`` rows starting at row ``phase`` and re-normalise."""
    f = f[phase:]
    n = len(f) // block
    if n == 0:
        return np.zeros((0, 12))
    d = f[: n * block].reshape(n, block, -1).mean(axis=1)
    norm = np.linalg.norm(d, axis=1, keepdims=True)
    return np.where(norm > 1e-9, d / np.maximum(norm, 1e-9), 0.0)


def _diagonal_run(a: np.ndarray, bs: list[np.ndarray]) -> tuple[int, int, int] | None:
    """Longest diagonal run of ``max_p a @ bs[p].T >= DISC_SIM`` (gaps <= DISC_GAP closed).

    Returns ``(i, j, length)`` in block rows of ``a`` and ``bs[0]``, or None.
    """
    n = len(a)
    m = min(len(b) for b in bs)
    if n == 0 or m == 0:
        return None
    sim = np.max([a @ b[:m].T for b in bs], axis=0)
    mask = sim >= DISC_SIM
    # Skew so row r is diagonal d = r - (n-1) (j = i + d); invalid cells are False.
    d = np.arange(-(n - 1), m)[:, None]
    i = np.arange(n)[None, :]
    j = i + d
    valid = (j >= 0) & (j < m)
    diag = mask[np.broadcast_to(i, j.shape), np.clip(j, 0, m - 1)] & valid
    # Close gaps of up to DISC_GAP cells: a cell is filled when a True lies within
    # DISC_GAP + 1 cells on both sides along the diagonal.
    g = DISC_GAP + 1
    pad = np.pad(diag, ((0, 0), (g, g)))
    left = np.zeros_like(diag)
    right = np.zeros_like(diag)
    for k in range(1, g + 1):
        left |= pad[:, g - k:g - k + n]
        right |= pad[:, g + k:g + k + n]
    closed = diag | (left & right & valid)
    # Longest run over all rows at once: flatten with a False separator per row.
    flat = np.pad(closed, ((0, 0), (0, 1))).ravel()
    run = _longest_run(flat)
    if run is None:
        return None
    first, last = run
    row, col = divmod(first, n + 1)
    return col, col + int(d[row, 0]), last - first + 1


def _regions(n_frames: int, block: int) -> dict[str, tuple[int, int]]:
    """Frame ranges (block aligned start) of the OP (first 40 %) and ED (last 25 %) regions."""
    op_end = int(n_frames * OP_REGION)
    ed_start = (int(n_frames * (1 - ED_REGION)) // block) * block
    return {"OP": (0, op_end), "ED": (ed_start, n_frames)}


def _pair_candidates(fa: np.ndarray, fb: np.ndarray, fps: float, min_len: float):
    block = max(1, round(fps / DISC_FPS))
    ra, rb = _regions(len(fa), block), _regions(len(fb), block)
    for kind in ("OP", "ED"):
        a0, a1 = ra[kind]
        b0, b1 = rb[kind]
        a = _downsample(fa[a0:a1], block)
        # Every phase of b, so an offset that is not a whole block still lines up.
        bs = [_downsample(fb[b0:b1], block, p) for p in range(block)]
        hit = _diagonal_run(a, bs)
        if hit is None:
            continue
        i, _, length = hit
        if length * block / fps < min_len:
            continue
        s = a0 + i * block
        e = min(len(fa), s + length * block)
        refined = _refine(fa, fb, s, e, fps)
        if refined is None:
            continue
        s, e = refined
        if (e - s) / fps < min_len:
            continue
        yield kind, s, e


def _refine(fa: np.ndarray, fb: np.ndarray, s: int, e: int, fps: float):
    """Full-fps edges of the coarse run ``fa[s:e]``: locate it in ``fb``, then locate
    that span back in ``fa``. ``find_reference`` trims edge frames that do not match."""
    pad = round(REFINE_PAD * fps)
    s, e = max(0, s - pad), min(len(fa), e + pad)
    mb = find_reference(fa[s:e], fb, fps)
    if mb is None:
        return None
    bs, be = round(mb.start * fps), round(mb.end * fps)
    ma = find_reference(fb[bs:be], fa, fps)
    if ma is None:
        return None
    return round(ma.start * fps), round(ma.end * fps)


def discover_references(features: dict[str, np.ndarray], fps: float = FPS,
                        min_len: float = 30.0) -> list[Candidate]:
    names = list(features)
    feats = {k: np.asarray(v, dtype=np.float64) for k, v in features.items()}
    n_eps = len(names)
    need = max(2, math.ceil(0.5 * n_eps))
    found: list[tuple[Candidate, np.ndarray]] = []
    for ka, kb in zip(names, names[1:]):
        for kind, s, e in _pair_candidates(feats[ka], feats[kb], fps, min_len):
            ref = feats[ka][s:e]
            support = 0
            for k in names:
                m = find_reference(ref, feats[k], fps)
                if m is not None and m.confidence >= WEAK:
                    support += 1
            if support >= need:
                found.append((Candidate(kind, ka, s / fps, e / fps, support), ref))

    # Dedupe within each kind: strongest support first, then the longest.
    found.sort(key=lambda cr: (-cr[0].support, -(cr[0].end - cr[0].start)))
    kept: list[tuple[Candidate, np.ndarray]] = []
    for c, ref in found:
        if any(k.kind == c.kind and _same(ref, kref, fps) for k, kref in kept):
            continue
        kept.append((c, ref))
    return sorted((c for c, _ in kept), key=lambda c: (c.kind, c.start))


def _same(a: np.ndarray, b: np.ndarray, fps: float) -> bool:
    for x, y in ((a, b), (b, a)):
        m = find_reference(x, y, fps)
        if m is not None and m.confidence >= HIGH:
            return True
    return False

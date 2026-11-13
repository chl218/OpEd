import functools
import time

import numpy as np
import pytest

from fixtures import OP_PCM, episode, episode_with, melody
from oped.fingerprint import FPS, SAMPLE_RATE, features_from_pcm
from oped.matching import Candidate, Match, discover_references, find_reference
from oped.project import HIGH, WEAK

SR = SAMPLE_RATE


def feats(pcm):
    return features_from_pcm(pcm)


@functools.lru_cache(maxsize=None)
def ep_feats(*args, **kw):
    return feats(episode(*args, **kw))


@functools.lru_cache(maxsize=None)
def ref_feats():
    return feats(OP_PCM)


def test_finds_op_anywhere_with_precise_edges():
    ref = ref_feats()                                  # 60 s OP alone
    for at in (0, 95.3, 600):
        m = find_reference(ref, ep_feats(at))
        assert m.start == pytest.approx(at, abs=0.3)
        assert m.end == pytest.approx(at + 60, abs=0.3)
        assert m.confidence >= HIGH


def test_shortened_op_and_sound_effects():
    m = find_reference(ref_feats(), ep_feats(120, op_trim=15, overlay=0.3))
    assert m.end == pytest.approx(120 + 45, abs=0.5)
    assert m.confidence >= WEAK


def test_absent_op_scores_low():
    m = find_reference(ref_feats(), ep_feats(None))
    assert m is None or m.confidence < WEAK


def test_op_at_file_start_partial():
    m = find_reference(ref_feats(), ep_feats(-10))   # first 10 s of OP missing
    assert m.start == pytest.approx(0, abs=0.3) and m.end == pytest.approx(50, abs=0.3)


# ---- edge cases beyond the brief ----

def test_op_at_very_end_of_episode():
    m = find_reference(ref_feats(), ep_feats(1240))  # OP is the last 60 s
    assert m.start == pytest.approx(1240, abs=0.3)
    assert m.end == pytest.approx(1300, abs=0.3)
    assert m.confidence >= HIGH


def test_op_cut_off_by_episode_end():
    m = find_reference(ref_feats(), ep_feats(1260))  # only the first 40 s fit
    assert m.start == pytest.approx(1260, abs=0.3)
    assert m.end == pytest.approx(1300, abs=0.3)


def test_empty_reference_or_too_short_episode():
    ref = ref_feats()
    assert find_reference(np.zeros((0, 12), np.float32), ep_feats(0)) is None
    assert find_reference(ref, ref[: len(ref) // 2 - 1]) is None
    assert find_reference(ref, np.zeros((0, 12), np.float32)) is None


def test_reference_longer_than_episode():
    ref = ref_feats()
    m = find_reference(ref, ref[100:])               # episode is 50 s of the 60 s OP
    assert m.start == pytest.approx(0, abs=0.3) and m.end == pytest.approx(50, abs=0.3)


def test_silent_episode_and_silent_reference_no_nan():
    ref = ref_feats()
    silent = np.zeros((13000, 12), np.float32)
    m = find_reference(ref, silent)
    assert m is None or (np.isfinite(m.confidence) and m.confidence < WEAK)
    m = find_reference(np.zeros_like(ref), ep_feats(0))
    assert m is None or (np.isfinite(m.confidence) and m.confidence < WEAK)


def test_silent_gaps_in_reference_are_finite():
    pcm = OP_PCM.copy()
    pcm[10 * SR:15 * SR] = 0                         # 5 s of silence inside the reference
    m = find_reference(feats(pcm), ep_feats(300))
    assert np.isfinite(m.start) and np.isfinite(m.end) and np.isfinite(m.confidence)
    # The longest run above threshold is the 45 s after the gap. Its start sits ~0.3 s
    # late: the 2 s average crosses 0.6 a few frames past a drop to s=0.
    assert m.start == pytest.approx(315, abs=0.5) and m.end == pytest.approx(360, abs=0.3)


def test_different_music_does_not_match():
    other = feats(melody(42, 60))
    m = find_reference(other, ep_feats(300))
    assert m is None or m.confidence < WEAK


def test_returns_match_dataclass_and_is_fast():
    ref, ep = ref_feats(), ep_feats(600)
    t0 = time.perf_counter()
    m = find_reference(ref, ep)
    dt = time.perf_counter() - t0
    assert isinstance(m, Match)
    assert dt < 1.5
    assert find_reference(ref, ep, fps=FPS * 2).start == pytest.approx(300, abs=0.15)


def test_sub_frame_offset_and_loud_overlay():
    m = find_reference(ref_feats(), ep_feats(95.35))  # OP starts mid-frame
    assert m.start == pytest.approx(95.35, abs=0.3) and m.end == pytest.approx(155.35, abs=0.3)
    assert m.confidence >= HIGH
    m = find_reference(ref_feats(), ep_feats(300, overlay=0.6))
    assert m.start == pytest.approx(300, abs=0.3) and m.end == pytest.approx(360, abs=0.3)
    assert m.confidence >= WEAK


# ---- reference discovery without hints ----

@functools.lru_cache(maxsize=None)
def epw_feats(*args, **kw):
    return feats(episode_with(*args, **kw))


def _season(spec):
    """spec: {name: (op_at, ed_at, seed[, kwargs])} -> features dict in insertion order."""
    out = {}
    for name, s in spec.items():
        kw = s[3] if len(s) > 3 else {}
        out[name] = epw_feats(s[0], s[1], s[2], **kw)
    return out


def test_discovers_op_and_ed_across_season():
    ep = {f"E{i}.mkv": feats(episode_with(op_at=30 + i * 7, ed_at=1200 + i, seed=i)) for i in range(1, 7)}
    ep["E1.mkv"] = feats(episode_with(op_at=None, ed_at=1201, seed=1))   # E1 has no OP
    c = discover_references(ep)
    assert [x.kind for x in c] == ["ED", "OP"]
    op = next(x for x in c if x.kind == "OP")
    assert op.end - op.start == pytest.approx(60, abs=1) and op.support >= 5


def test_no_repeats_no_candidates():
    assert discover_references({f"E{i}": feats(episode(None, seed=i)) for i in range(4)}) == []


def test_candidate_times_are_in_its_episode():
    spec = {f"E{i}": (30 + i * 7, 1200 + i, i) for i in range(1, 5)}
    c = discover_references(_season(spec))
    assert [x.kind for x in c] == ["ED", "OP"]
    for x in c:
        assert isinstance(x, Candidate)
        op_at, ed_at, _ = spec[x.episode]
        at, length = (ed_at, 70) if x.kind == "ED" else (op_at, 60)
        assert x.start == pytest.approx(at, abs=1)
        assert x.end == pytest.approx(at + length, abs=1)
        assert x.support == 4


def test_empty_and_single_episode_season():
    assert discover_references({}) == []
    assert discover_references(_season({"E1": (40, 1200, 1)})) == []


def test_two_episode_season():
    c = discover_references(_season({"E1": (40, 1200, 1), "E2": (95, 1180, 2)}))
    assert [(x.kind, x.episode, x.support) for x in c] == [("ED", "E1", 2), ("OP", "E1", 2)]
    op = c[1]
    assert op.start == pytest.approx(40, abs=1) and op.end == pytest.approx(100, abs=1)


def test_op_at_sub_block_offsets():
    # Offsets that are not multiples of the 0.5 s discovery block (nor of a 0.1 s frame).
    spec = {"E1": (30.33, None, 1), "E2": (101.62, None, 2), "E3": (55.17, None, 3),
            "E4": (200.88, None, 4)}
    c = discover_references(_season(spec))
    assert [x.kind for x in c] == ["OP"]
    op = c[0]
    assert op.support == 4
    assert op.start == pytest.approx(spec[op.episode][0], abs=1)
    assert op.end - op.start == pytest.approx(60, abs=1)


def test_shortened_op_in_one_episode():
    spec = {"E1": (40, 1200, 1), "E2": (50, 1200, 2, {"op_trim": 15}),
            "E3": (60, 1200, 3), "E4": (70, 1200, 4)}
    c = discover_references(_season(spec))
    assert [x.kind for x in c] == ["ED", "OP"]
    op = c[1]
    assert op.support == 4
    assert op.end - op.start == pytest.approx(60, abs=1)


def test_episode_without_op_or_ed_breaks_pairs_but_not_discovery():
    # E3 (a recap) has neither OP nor ED, so pairs E2-E3 and E3-E4 find nothing.
    spec = {"E1": (40, 1200, 1), "E2": (45, 1205, 2), "E3": (None, None, 3),
            "E4": (50, 1210, 4), "E5": (55, 1215, 5)}
    c = discover_references(_season(spec))
    assert [(x.kind, x.support) for x in c] == [("ED", 4), ("OP", 4)]


def test_short_episodes_do_not_crash():
    spec = {"E1": (None, None, 1, {"total": 20}), "E2": (None, None, 2, {"total": 3}),
            "E3": (None, None, 3, {"total": 0.05})}
    assert discover_references(_season(spec)) == []
    short = {"E1": (5, None, 1, {"total": 100}), "E2": (10, None, 2, {"total": 100})}
    c = discover_references(_season(short))   # OP spans more than the 40 % region
    assert all(x.kind == "OP" for x in c)


def test_discovery_is_fast():
    ep = {f"E{i}": epw_feats(30 + i * 7, 1200 + i, i) for i in range(1, 7)}
    t0 = time.perf_counter()
    discover_references(ep)
    assert time.perf_counter() - t0 < 10


def _diag_with_gap(gap, n=40):
    """a and b identical except ``gap`` blocks (rows 10..) of b are orthogonal to a."""
    rng = np.random.default_rng(0)
    a = rng.standard_normal((n, 12))
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b = a.copy()
    for r in range(10, 10 + gap):
        v = rng.standard_normal(12)
        v -= (v @ a[r]) * a[r]        # orthogonal to a[r]: similarity 0 on the diagonal
        b[r] = v / np.linalg.norm(v)
    return a, b


# A gap of 3 splits the 40-block diagonal into 10 | gap | 27.
@pytest.mark.parametrize("gap,expected", [(1, 40), (2, 40), (3, 27)])
def test_diagonal_run_closes_gaps_of_at_most_disc_gap(gap, expected):
    from oped.matching import DISC_GAP, _diagonal_run
    assert DISC_GAP == 2
    a, b = _diag_with_gap(gap)
    i, j, length = _diagonal_run(a, [b])
    assert length == expected and i == j

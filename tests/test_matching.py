import functools
import time

import numpy as np
import pytest

from fixtures import OP_PCM, episode, melody
from oped.fingerprint import FPS, SAMPLE_RATE, features_from_pcm
from oped.matching import Match, find_reference
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

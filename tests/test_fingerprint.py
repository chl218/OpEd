import numpy as np
import pytest

from fixtures import make_color_video, melody
from oped import fingerprint
from oped.fingerprint import SAMPLE_RATE, episode_features, features_from_pcm
from oped.media import probe


def test_shape_and_normalization():
    f = features_from_pcm(melody(1, 30))
    assert f.shape == (300, 12) and f.dtype == np.float32
    nz = f[np.abs(f).sum(1) > 0]
    assert np.allclose(np.linalg.norm(nz, axis=1), 1, atol=1e-4)
    assert np.allclose(nz.sum(1), 0, atol=1e-4)


def test_silence_is_zero():
    assert not features_from_pcm(np.zeros(SAMPLE_RATE * 5, np.float32)).any()


def test_same_music_matches_different_doesnt():
    a, b, c = (features_from_pcm(melody(s, 20)) for s in (1, 1, 2))
    assert (a * b).sum(1).mean() > 0.95
    assert (a * c).sum(1).mean() < 0.4


def test_cache_reused(tools, tmp_path, monkeypatch):
    src = make_color_video(tools, tmp_path / "e.mkv", [("red", 5)])
    info = probe(tools, src)
    first = episode_features(tools, info, tmp_path / "cache")
    monkeypatch.setattr(fingerprint, "decode_audio", lambda *a: pytest.fail("should use cache"))
    assert np.array_equal(episode_features(tools, info, tmp_path / "cache"), first)


def test_corrupt_cache_recomputed(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "e.mkv", [("red", 3)])
    info = probe(tools, src)
    cache = tmp_path / "cache"
    first = episode_features(tools, info, cache)
    for p in cache.glob("*.npy"):
        p.write_bytes(b"garbage")
    assert np.array_equal(episode_features(tools, info, cache), first)
    assert not list(cache.glob("*.tmp"))


def test_no_cache_dir_and_long_input_speed():
    import time
    pcm = melody(3, 24 * 60)
    t = time.perf_counter()
    f = features_from_pcm(pcm)
    assert f.shape == (len(pcm) // 1200, 12)
    assert time.perf_counter() - t < 10

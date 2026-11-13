"""Chroma audio fingerprints with an on-disk cache."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import platformdirs

from oped.media import MediaInfo, Tools, run

SAMPLE_RATE = 12000
HOP = 1200
WIN = 4096
FPS = SAMPLE_RATE / HOP  # 10.0
_BATCH = 2048


def _pitch_map(sr: int) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    sel = np.where((freqs >= 55.0) & (freqs <= 5000.0))[0]
    pc = np.round(12 * np.log2(freqs[sel] / 440.0)).astype(int) % 12
    return sel, pc


def features_from_pcm(pcm: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    pcm = np.asarray(pcm, dtype=np.float32)
    n = len(pcm) // HOP
    out = np.zeros((n, 12), np.float32)
    if n == 0:
        return out
    sel, pc = _pitch_map(sr)
    fold = np.zeros((len(sel), 12), np.float32)
    fold[np.arange(len(sel)), pc] = 1.0
    win = np.hanning(WIN).astype(np.float32)
    half = WIN // 2
    padded = np.pad(pcm, (half, half + HOP))
    for s in range(0, n, _BATCH):
        e = min(n, s + _BATCH)
        idx = (np.arange(s, e) * HOP)[:, None] + np.arange(WIN)[None, :]
        frames = padded[idx]  # frame i covers [i*HOP-half, i*HOP+half)
        rms = np.sqrt((frames.astype(np.float64) ** 2).mean(1))
        mag = np.abs(np.fft.rfft(frames * win, axis=1))[:, sel].astype(np.float32)
        chroma = mag @ fold
        chroma -= chroma.mean(1, keepdims=True)
        norm = np.linalg.norm(chroma, axis=1, keepdims=True)
        ok = (rms >= 1e-4) & (norm[:, 0] > 1e-12)
        chroma = np.where(ok[:, None], chroma / np.maximum(norm, 1e-12), 0.0)
        out[s:e] = chroma
    return out


def decode_audio(tools: Tools, info: MediaInfo) -> np.ndarray:
    stream = info.primary_audio()
    if stream is None:
        raise ValueError(f"No audio stream in {info.path}")
    proc = run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-i", str(info.path),
        "-map", f"0:{stream.index}", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ])
    data = proc.stdout
    return np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4").astype(np.float32)


def default_cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir("oped")) / "fingerprints"


def _cache_key(path: Path) -> str:
    p = Path(path)
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|v1"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def episode_features(tools: Tools, info: MediaInfo, cache_dir: Path | None) -> np.ndarray:
    target = None
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        target = cache_dir / f"{_cache_key(info.path)}.npy"
        if target.exists():
            try:
                arr = np.load(target, allow_pickle=False)
                if arr.ndim == 2 and arr.shape[1] == 12 and arr.dtype == np.float32:
                    return arr
            except Exception:
                pass  # corrupt cache: recompute
    feats = features_from_pcm(decode_audio(tools, info))
    if target is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "wb") as fh:
                np.save(fh, feats)
            os.replace(tmp, target)
        except OSError:
            pass
    return feats

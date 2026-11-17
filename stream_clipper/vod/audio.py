"""Audio features on a 1-second grid from a 16 kHz mono WAV (numpy only)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

FRAME = 0.1  # seconds per analysis frame


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, n, width, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("expected 16-bit PCM wav")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def frame_db(x: np.ndarray, sr: int, frame: float = FRAME) -> np.ndarray:
    hop = int(sr * frame)
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)
    return 20 * np.log10(rms + 1e-7)


def rolling_median(v: np.ndarray, win: int) -> np.ndarray:
    """Median over a centred window, computed on a coarse grid for speed (good enough for a baseline)."""
    if len(v) == 0:
        return v
    step = max(1, win // 8)
    centers = np.arange(0, len(v), step)
    meds = np.array([np.median(v[max(0, c - win // 2): c + win // 2 + 1]) for c in centers])
    return np.interp(np.arange(len(v)), centers, meds)


def per_second(frames: np.ndarray, reduce=np.mean, frame: float = FRAME) -> np.ndarray:
    k = int(round(1 / frame))
    n = len(frames) // k
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return reduce(frames[: n * k].reshape(n, k), axis=1)


def features(x: np.ndarray, sr: int, speech_mask: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """energy: loudness above rolling 60 s median (dB);
    shouting: sustained (>=1.5 s) loud speech;
    laughter: 3-8 Hz modulation of the loudness envelope (rhythmic 'ha-ha-ha' bursts), heuristic."""
    db = frame_db(x, sr)
    base = rolling_median(db, int(60 / FRAME))
    rel = np.clip(db - base, 0, None)
    energy = per_second(rel)
    n = len(energy)

    loud = per_second((rel > 6).astype(np.float32))           # fraction of the second that is loud
    sustained = np.convolve(loud, np.ones(2) / 2, mode="same")  # 2 s smoothing
    if speech_mask is not None and len(speech_mask) >= n:
        sustained = sustained * speech_mask[:n]
    shouting = np.where(sustained >= 0.75, sustained * energy, 0.0)

    # laughter-like: envelope modulation in 3-8 Hz within 2 s windows (needs 50 Hz envelope)
    hop = sr // 50
    m = len(x) // hop
    env = np.sqrt(np.mean(x[: m * hop].reshape(m, hop) ** 2, axis=1) + 1e-10) if m else np.zeros(0)
    laugh = np.zeros(n, dtype=np.float32)
    w = 100  # 2 s of 50 Hz envelope
    freqs = np.fft.rfftfreq(w, d=1 / 50)
    band = (freqs >= 3) & (freqs <= 8)
    for s in range(n):
        seg = env[max(0, s * 50 - 25): max(0, s * 50 - 25) + w]
        if len(seg) < w or seg.mean() < 1e-3:
            continue
        spec = np.abs(np.fft.rfft(seg - seg.mean()))
        tot = spec[1:].sum() + 1e-9
        laugh[s] = spec[band].sum() / tot
    laugh = np.where(energy > 3, laugh, 0.0)
    return {"audio_energy": energy.astype(np.float32), "shouting": shouting.astype(np.float32), "laughter": laugh}

"""faster-whisper wrapper. Models are loaded once per process and cached in ctx.cache."""
from __future__ import annotations

import time
from pathlib import Path

from .. import log

L = log.get("whisper")

# segments with these values are almost always hallucinations on music/silence
MAX_NO_SPEECH = 0.6
MIN_AVG_LOGPROB = -1.0


def get_model(ctx, name: str):
    key = f"whisper:{name}"
    if key not in ctx.cache:
        from faster_whisper import WhisperModel

        w = ctx.cfg.g.whisper
        t0 = time.time()
        ctx.cache[key] = WhisperModel(name, device=w.device, compute_type=w.compute_type, cpu_threads=ctx.threads,
                                      num_workers=1, download_root=str(Path(ctx.cfg.g.models_dir) / "whisper"))
        L.info("whisper model loaded", extra={"model": name, "secs": round(time.time() - t0, 1), "worker": ctx.name})
    return ctx.cache[key]


def transcribe(ctx, audio: Path, *, language: str = "auto", model: str | None = None, task: str = "transcribe") -> dict:
    w = ctx.cfg.g.whisper
    m = get_model(ctx, model or w.model)
    t0 = time.time()
    # pass samples, not a path: faster-whisper's own decoder (PyAV) breaks across versions;
    # our audio is always a 16 kHz mono WAV produced by ffmpeg
    from ..vod.audio import load_wav

    samples, sr = load_wav(Path(audio))
    if sr != 16000:
        raise ValueError(f"expected 16 kHz audio, got {sr}")
    segs, info = m.transcribe(
        samples, language=None if language in ("auto", "", None) else language, task=task,
        beam_size=w.beam_size, vad_filter=w.vad, word_timestamps=True, condition_on_previous_text=False,
    )
    out = []
    for s in segs:
        if s.no_speech_prob > MAX_NO_SPEECH and s.avg_logprob < MIN_AVG_LOGPROB:
            continue
        out.append({
            "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
            "avg_logprob": round(s.avg_logprob, 3),
            "words": [{"start": round(x.start, 3), "end": round(x.end, 3), "word": x.word.strip(), "prob": round(x.probability, 3)}
                      for x in (s.words or []) if x.word.strip()],
        })
    L.info("transcribed", extra={"audio": str(audio), "lang": info.language, "secs": round(time.time() - t0, 1),
                                 "audio_secs": round(info.duration, 1), "segments": len(out)})
    return {"language": info.language, "duration": info.duration, "segments": out}

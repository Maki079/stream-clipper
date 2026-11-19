"""VOD highlight analysis: features -> score -> top-N windows (snapped to sentences)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import log
from ..config import LlmCfg, SourceCfg
from . import audio, llm, scenes, scoring, text

L = log.get("analyze")


def vod_length_limits(src: SourceCfg, campaigns: list) -> tuple[int, int]:
    lo = src.vod.min_seconds or max([c.min_seconds for c in campaigns] or [61])
    hi = src.vod.max_seconds or min([c.max_seconds for c in campaigns] or [90])
    return int(lo), int(max(hi, lo))


def compute_features(wav: Path, video: Path | None, segments: list[dict], src: SourceCfg, threads: int = 4) -> dict[str, np.ndarray]:
    x, sr = audio.load_wav(wav)
    n = len(x) // sr
    mask = text.speech_mask(segments, n)
    feats = audio.features(x, sr, mask)
    feats.update(text.features(segments, n, src.vod.keywords))
    if video is not None and src.vod.weights.scene_density > 0:
        try:
            feats["scene_density"] = scenes.density(scenes.scene_changes(video, src.vod.scene_threshold, threads), n)
        except Exception as e:  # noqa: BLE001 - scene detection is a bonus feature
            L.warning("scene detection skipped", extra={"error": str(e)[:300]})
    return feats


def select_windows(feats: dict[str, np.ndarray], segments: list[dict], src: SourceCfg, min_len: int, max_len: int,
                   llm_cfg: LlmCfg | None = None, creator: str = "", *,
                   language: str = "auto") -> tuple[list[scoring.Window], np.ndarray]:
    score = scoring.combine(feats, src.vod.weights.model_dump())
    duration = float(len(score))
    if duration < min_len:
        return [], score
    n = src.vod.top_n
    use_llm = llm_cfg is not None and llm_cfg.enabled
    picked = scoring.select_top(score, min_len, max_len, max(n, llm_cfg.top_k_candidates) if use_llm else n)
    for w in picked:
        w.detail = {name: round(float(v[int(w.start):int(w.end)].mean()), 3) for name, v in feats.items()}
        w.detail["heuristic"] = round(w.score, 3)
    if use_llm:
        for w in picked:
            res = llm.rate(llm_cfg, " ".join(text.sentences(segments, w.start, w.end)), creator,
                           language=src.language if src.language != "auto" else language)
            if res:
                w.detail["llm_score"] = res[0]
                w.hook = res[1] or None
                w.score += llm_cfg.weight * (res[0] - 5) / 2.5
        picked.sort(key=lambda w: -w.score)
        picked = picked[:n]
    out = [scoring.snap(w, segments, min_len, max_len, duration=duration) for w in picked]
    return out, score

"""Optional local LLM (Ollama) rating of candidate windows. Off by default."""
from __future__ import annotations

import json

import httpx

from .. import log
from ..config import LlmCfg

L = log.get("llm")

PROMPT = """You rate livestream transcript excerpts for short-form video (TikTok/Reels/Shorts).
Rate how likely this excerpt is to make a viral, self-contained short (0 = boring, 10 = must post).
Also write a short, punchy English hook title (max 8 words, no hashtags, no quotes).
Answer ONLY with JSON: {{"score": <0-10>, "hook": "<title>"}}

Creator: {creator}
Excerpt:
\"\"\"{text}\"\"\"
"""


def rate(cfg: LlmCfg, text: str, creator: str = "") -> tuple[float, str] | None:
    """(score 0-10, hook) or None if the LLM is unavailable/garbled (never fatal)."""
    if not cfg.enabled or not text.strip():
        return None
    try:
        r = httpx.post(f"{cfg.ollama_url.rstrip('/')}/api/generate", timeout=cfg.timeout_seconds, json={
            "model": cfg.model, "prompt": PROMPT.format(creator=creator, text=text[:4000]), "stream": False,
            "format": "json", "options": {"temperature": 0.2, "num_predict": 80}})
        r.raise_for_status()
        d = json.loads(r.json().get("response", "{}"))
        score = float(d.get("score", 0))
        hook = str(d.get("hook", "")).strip().strip('"')[:80]
        return max(0.0, min(10.0, score)), hook
    except Exception as e:  # noqa: BLE001
        L.warning("llm rating failed", extra={"error": str(e)[:300]})
        return None


def hook(cfg: LlmCfg, text: str, creator: str = "") -> str | None:
    res = rate(cfg, text, creator)
    return res[1] if res and res[1] else None

"""Per-campaign caption text + rule validation."""
from __future__ import annotations

import hashlib
import re

from .config import PLACEHOLDER, CampaignCfg

DEFAULT_HOOKS = [
    "{creator} did NOT expect this 😭",
    "Chat could not believe this 💀",
    "This is why we watch {creator} 😂",
    "Wait for it… 👀",
    "{creator} lost it here 😭",
    "Nobody was ready for this 💀",
    "The way {creator} reacted 😂",
    "This moment broke the stream 🔥",
]


def _pick(options: list[str], seed: str) -> str:
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


def display_name(c: CampaignCfg) -> str:
    """Readable creator name for hooks: '@kaicenat' -> 'Kai Cenat' if an alias exists, else 'kaicenat'."""
    for a in c.creator_aliases:
        if " " in a:
            return a.title()
    return c.creator_name.lstrip("@") if PLACEHOLDER not in c.creator_name else "they"


def build_caption(clip_id: str, c: CampaignCfg, *, hook: str | None = None, quote: str | None = None) -> str:
    templates = c.hook_templates or DEFAULT_HOOKS
    line1 = (hook or _pick(templates, clip_id)).replace("{creator}", display_name(c)).strip()
    parts = [line1]
    if quote:
        q = quote.strip().strip('"')
        if len(q) <= 120:
            parts.append(f"“{q}”")
    parts.append(c.creator_name)
    tags = list(dict.fromkeys(c.required_hashtags + c.extra_hashtags))
    parts.append(" ".join(tags))
    text = "\n".join(p for p in parts if p)
    if len(text) > c.max_caption_chars and quote:
        return build_caption(clip_id, c, hook=hook, quote=None)
    return text


def validate(text: str, c: CampaignCfg) -> list[str]:
    problems = []
    low = text.lower()
    for tag in c.required_hashtags:
        if not re.search(re.escape(tag.lower()) + r"(?![\w])", low):
            problems.append(f"missing required hashtag {tag}")
    if c.creator_name.lower() not in low:
        problems.append(f"missing creator mention {c.creator_name}")
    if len(text) > c.max_caption_chars:
        problems.append(f"caption too long ({len(text)} > {c.max_caption_chars})")
    if PLACEHOLDER in text:
        problems.append(f"campaign still has {PLACEHOLDER} placeholders: fill them from the brief")
    return problems


def caption_file_text(text: str, problems: list[str], c: CampaignCfg) -> str:
    """What goes into the .txt: ready-to-paste caption; problems are flagged loudly on top."""
    out = ""
    if problems:
        out += "!!! DO NOT POST AS IS - fix first:\n" + "\n".join(f"!!! - {p}" for p in problems) + "\n\n"
    out += text + "\n"
    if c.notes:
        out += f"\n---\nCampaign notes: {c.notes}\nAllowed platforms: {', '.join(c.allowed_platforms)}\n"
    return out


def suggest_campaigns(campaigns: dict[str, CampaignCfg], tagged: list[str], haystack: str) -> list[str]:
    """Other campaigns whose creator aliases appear in transcript/chat (suggestions only)."""
    low = " " + re.sub(r"[^\w@ ]+", " ", haystack.lower()) + " "
    out = []
    for name, c in campaigns.items():
        if name in tagged:
            continue
        names = [a.lower() for a in c.creator_aliases] + ([c.creator_name.lower()] if PLACEHOLDER not in c.creator_name else [])
        if any(f" {a.strip()} " in low or f" {a.strip().lstrip('@')} " in low for a in names if a.strip()):
            out.append(name)
    return out

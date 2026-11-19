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

LOCALIZED_HOOKS = {
    "en": DEFAULT_HOOKS,
    "pl": ["Tego nikt się nie spodziewał 😭", "{creator} i ten moment 🔥", "Warto było na to czekać 👀"],
    "sk": ["Toto nikto nečakal 😭", "{creator} a tento moment 🔥"],
    "cs": ["Tohle nikdo nečekal 😭", "{creator} a tenhle moment 🔥"],
    "de": ["Damit hat niemand gerechnet 😭", "Dieser Moment mit {creator} 🔥"],
    "fr": ["Personne ne s’attendait à ça 😭", "Ce moment avec {creator} 🔥"],
    "es": ["Nadie se esperaba esto 😭", "Este momento de {creator} 🔥"],
    "it": ["Nessuno se lo aspettava 😭", "Questo momento con {creator} 🔥"],
    "pt": ["Ninguém esperava por isto 😭", "Este momento de {creator} 🔥"],
    "nl": ["Dit had niemand verwacht 😭", "Dit moment met {creator} 🔥"],
    "uk": ["Цього ніхто не очікував 😭", "Цей момент із {creator} 🔥"],
    "ru": ["Этого никто не ожидал 😭", "Этот момент с {creator} 🔥"],
    "tr": ["Bunu kimse beklemiyordu 😭", "{creator} ile bu an 🔥"],
    "ar": ["لم يتوقع أحد هذا 😭", "هذه اللحظة مع {creator} 🔥"],
    "zh": ["谁也没想到这一幕 😭", "{creator} 的精彩瞬间 🔥"],
    "ja": ["誰も予想していなかった展開 😭", "{creator} のこの瞬間 🔥"],
    "ko": ["아무도 예상 못 한 순간 😭", "{creator}의 이 순간 🔥"],
    "hi": ["किसी ने इसकी उम्मीद नहीं की थी 😭", "{creator} का यह पल 🔥"],
    "sv": ["Det här hade ingen väntat sig 😭", "Det här ögonblicket med {creator} 🔥"],
    "no": ["Dette hadde ingen ventet 😭", "Dette øyeblikket med {creator} 🔥"],
    "da": ["Det havde ingen forventet 😭", "Dette øjeblik med {creator} 🔥"],
    "fi": ["Tätä ei kukaan odottanut 😭", "Tämä hetki ja {creator} 🔥"],
    "ro": ["Nimeni nu se aștepta la asta 😭", "Acest moment cu {creator} 🔥"],
    "hu": ["Erre senki sem számított 😭", "Ez a pillanat: {creator} 🔥"],
    "bg": ["Никой не очакваше това 😭", "Този момент с {creator} 🔥"],
    "id": ["Tidak ada yang menyangka ini 😭", "Momen ini bersama {creator} 🔥"],
    "vi": ["Không ai ngờ điều này 😭", "Khoảnh khắc này của {creator} 🔥"],
}


def resolve_language(source: str | None = None, detected: str | None = None, fallback: str = "en") -> str:
    """Prefer the streamer's configured language, then speech detection, then campaign fallback."""
    for value in (source, detected, fallback, "en"):
        code = (value or "").strip().lower().replace("_", "-").split("-")[0]
        if code not in ("", "auto", "und", "unknown"):
            return code
    return "en"


def _pick(options: list[str], seed: str) -> str:
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


def display_name(c: CampaignCfg) -> str:
    """Readable creator name for hooks: '@kaicenat' -> 'Kai Cenat' if an alias exists, else 'kaicenat'."""
    for a in c.creator_aliases:
        if " " in a:
            return a.title()
    return c.creator_name.lstrip("@") if PLACEHOLDER not in c.creator_name else "they"


def build_caption(clip_id: str, c: CampaignCfg, *, hook: str | None = None, quote: str | None = None,
                  language: str | None = None) -> str:
    language = resolve_language(language, fallback=c.caption_language)
    # For other languages, keep the heading neutral and use the original speech
    # quote below rather than introducing an English sentence.
    templates = c.hook_templates or LOCALIZED_HOOKS.get(language, ["🎬 {creator}"])
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
        return build_caption(clip_id, c, hook=hook, quote=None, language=language)
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

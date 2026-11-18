"""Notifications (Telegram bot or ntfy): new-clip batches and a daily summary."""
from __future__ import annotations

import datetime as dt
import html
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import log
from .config import NotificationsCfg

L = log.get("notify")


class TelegramError(RuntimeError):
    pass


def tg_call(cfg: NotificationsCfg, method: str, *, timeout: float = 30, files: dict | None = None, **params) -> object:
    url = f"https://api.telegram.org/bot{cfg.telegram.bot_token}/{method}"
    if files:
        r = httpx.post(url, data={k: str(v) for k, v in params.items()}, files=files, timeout=timeout)
    else:
        r = httpx.post(url, json=params, timeout=timeout)
    try:
        js = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if not js.get("ok"):
        # never log the URL: it contains the bot token
        raise TelegramError(f"{method}: {js.get('error_code')} {js.get('description')}")
    return js["result"]


def telegram_chat_id(cfg: NotificationsCfg, store=None) -> str | None:
    """Configured chat id, else the one remembered in the DB, else the newest chat that wrote to the bot."""
    if cfg.telegram.chat_id:
        return cfg.telegram.chat_id
    if store is not None and (cid := store.kv_get("telegram_chat_id")):
        return cid
    updates = tg_call(cfg, "getUpdates", timeout=30)
    chats = [u["message"]["chat"] for u in updates if "message" in u and u["message"].get("chat")]
    private = [c for c in chats if c.get("type") == "private"] or chats
    if not private:
        return None
    cid = str(private[-1]["id"])
    if store is not None:
        store.kv_set("telegram_chat_id", cid)
    L.info("telegram chat discovered", extra={"chat": private[-1].get("username") or private[-1].get("title") or cid})
    return cid


def send(cfg: NotificationsCfg, title: str, body: str, store=None, silent: bool = False) -> bool:
    if cfg.backend == "none":
        return False
    try:
        if cfg.backend == "telegram":
            chat = telegram_chat_id(cfg, store)
            if not chat:
                L.warning("telegram: no chat yet - open your bot in Telegram and press Start")
                return False
            text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
            for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
                tg_call(cfg, "sendMessage", chat_id=chat, text=chunk, parse_mode="HTML",
                        disable_web_page_preview=True, disable_notification=silent)
        else:
            headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": "movie_camera"}
            if cfg.ntfy.token:
                headers["Authorization"] = f"Bearer {cfg.ntfy.token}"
            if silent:
                headers["Priority"] = "low"
            r = httpx.post(f"{cfg.ntfy.server.rstrip('/')}/{cfg.ntfy.topic}", content=body.encode(), headers=headers, timeout=30)
            r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        L.warning("notification failed", extra={"backend": cfg.backend, "error": str(e)[:300]})
        return False


def format_clip(c: dict) -> str:
    m = c["meta"]
    lines = [f"⭐ {c['score']:.1f}  {c['id']}  ({m.get('duration', 0):.0f}s, {c['source']})"]
    for d in m.get("deliveries", []):
        lines.append(f"  [{d['campaign']}] {d.get('link') or d['location']}")
    cap = m.get("captions", {})
    if cap:
        first = next(iter(cap.values()))
        lines.append("  " + first["text"].replace("\n", " | ")[:300])
        if any(v["problems"] for v in cap.values()):
            lines.append("  ⚠️ caption needs fixing before posting (see .txt)")
    if m.get("suggested_campaigns"):
        lines.append(f"  maybe also: {', '.join(m['suggested_campaigns'])}")
    return "\n".join(lines)


def new_clips_message(clips: list[dict], top_n: int) -> tuple[str, str]:
    clips = sorted(clips, key=lambda c: -c["score"])
    body = "\n\n".join(format_clip(c) for c in clips[:top_n])
    if len(clips) > top_n:
        body += f"\n\n…and {len(clips) - top_n} more"
    return f"{len(clips)} new clip(s) ready", body


def daily_summary(clips: list[dict], failed_jobs: int) -> tuple[str, str]:
    by_src: dict[str, int] = {}
    by_camp: dict[str, int] = {}
    for c in clips:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
        for k in c["campaigns"]:
            by_camp[k] = by_camp.get(k, 0) + 1
    delivered = [c for c in clips if c["status"] == "delivered"]
    lines = [f"Clips delivered: {len(delivered)} / created: {len(clips)}",
             "By source: " + (", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) or "-"),
             "By campaign: " + (", ".join(f"{k} {v}" for k, v in sorted(by_camp.items())) or "-"),
             f"Failed jobs (24h): {failed_jobs}"]
    top = sorted(delivered, key=lambda c: -c["score"])[:3]
    if top:
        lines += ["", "Top clips:"] + [format_clip(c) for c in top]
    return "Daily stream-clipper summary", "\n".join(lines)


def summary_due(cfg: NotificationsCfg, tz: str, last_sent_date: str | None, now: float | None = None) -> str | None:
    """Returns today's date string if the summary should be sent now."""
    t = dt.datetime.fromtimestamp(now or time.time(), ZoneInfo(tz))
    hh, mm = (int(x) for x in cfg.daily_summary_time.split(":"))
    today = t.date().isoformat()
    if (t.hour, t.minute) >= (hh, mm) and last_sent_date != today:
        return today
    return None


def clip_header(c: dict) -> str:
    m = c["meta"]
    lines = [f"🎬 <b>{html.escape(c['id'])}</b>", f"⭐ {c['score']:.1f} · {m.get('duration', 0):.0f}s · {html.escape(c['source'])}"]
    sp = m.get("spikes") or []
    if sp:
        best = max(sp, key=lambda s: s.get("peak_messages", 0))
        if best.get("baseline"):
            lines.append(f"💬 chat {best['peak_messages']} msgs/10s = {best['peak_messages'] / max(best['baseline'], 1):.1f}x average")
    lj = m.get("llm") or {}
    if lj.get("score") is not None:
        lines.append(f"🤖 {lj['score']:.0f}/10 · {html.escape(str(lj.get('reason', '')))}")
    if m.get("suggested_campaigns"):
        lines.append("maybe also: " + html.escape(", ".join(m["suggested_campaigns"])))
    return "\n".join(lines)


def send_clip_telegram(cfg: NotificationsCfg, store, clip: dict, video: Path, chat: str) -> None:
    """Video first, then one copy-ready message per campaign caption (tap the block to copy)."""
    info = {}
    try:
        from .media.render import probe
        info = probe(video)
    except Exception:  # noqa: BLE001
        pass
    with video.open("rb") as f:
        tg_call(cfg, "sendVideo", timeout=600, files={"video": (video.name, f, "video/mp4")}, chat_id=chat,
                caption=clip_header(clip)[:1000], parse_mode="HTML", supports_streaming="true",
                width=info.get("width", 1080), height=info.get("height", 1920), duration=int(info.get("duration", 0)))
    for name, cap in (clip["meta"].get("captions") or {}).items():
        warn = "\n⚠️ " + html.escape("; ".join(cap["problems"])) if cap.get("problems") else ""
        tg_call(cfg, "sendMessage", chat_id=chat, parse_mode="HTML", disable_notification=True,
                text=f"<b>[{html.escape(name)}]</b>{warn}\n<pre>{html.escape(cap['text'])}</pre>")

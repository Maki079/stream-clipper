"""Job handlers. Each one is idempotent and enqueues the next step.

LIVE: cut -> render -> deliver
VOD : ingest -> transcribe_full -> analyze_vod -> (per window) render -> deliver
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import captions, delivery, log, notify
from .context import Context
from .live import buffer
from .media import motion
from .media import render as media
from .media import whisper
from .media.ass import Word, build_ass
from .queue import PRIORITY_LIVE, PRIORITY_VOD, PermanentError
from .vod import analyze, ingest
from .vod import llm as llm_mod
from .vod import text as vtext

L = log.get("pipeline")


class NotReady(Exception):
    """Footage not complete yet; retried with backoff."""


def _src(ctx: Context, name: str):
    s = ctx.cfg.sources.get(name)
    if s is None:
        raise PermanentError(f"source {name!r} no longer in config")
    return s


def _clip(ctx: Context, clip_id: str) -> dict:
    c = ctx.store.get_clip(clip_id)
    if c is None:
        raise PermanentError(f"clip {clip_id} not found")
    return c


def _prio(clip: dict) -> int:
    return PRIORITY_LIVE if clip["mode"] == "live" else PRIORITY_VOD


# ---------------------------------------------------------------------- live


def handle_cut(ctx: Context, p: dict) -> None:
    clip = _clip(ctx, p["clip_id"])
    if clip["status"] != "pending":
        return
    start, end = clip["start_ts"], clip["end_ts"]
    segs = buffer.select(buffer.list_segments(ctx.buffer_dir(clip["source"])), start, end)
    complete, gaps = buffer.coverage(segs, start, end)
    if not complete and not p.get("final") and time.time() < end + 180:
        raise NotReady(f"buffer does not cover clip yet (gaps {gaps})")
    if not segs:
        ctx.store.update_clip(clip["id"], status="failed", meta_update={"error": "no footage in buffer"})
        raise PermanentError("no buffer segments for clip window (buffer pruned or stream down)")
    raw = media.concat_segments([s.path for s in segs], ctx.work_dir(clip["id"]) / "raw.ts")
    offset = max(0.0, start - segs[0].start)
    duration = min(end, segs[-1].end) - max(start, segs[0].start)
    ctx.store.update_clip(clip["id"], status="cut", meta_update={
        "raw": str(raw), "raw_is_temp": True, "offset": round(offset, 3), "duration": round(duration, 3),
        "gaps": [[round(a, 1), round(b, 1)] for a, b in gaps]})
    ctx.queue.enqueue("render", {"clip_id": clip["id"]}, priority=PRIORITY_LIVE, dedupe_key=f"render:{clip['id']}")


# -------------------------------------------------------------------- render


def _chat_moments(clip: dict, offset: float) -> list[float]:
    """Spike onsets (minus chat delay) as seconds from the start of the rendered clip."""
    m = clip["meta"]
    if clip["mode"] != "live":
        return []
    delay = float(m.get("chat_delay_seconds") or 0)
    out = []
    for s in m.get("spikes", []):
        t = s.get("start", s.get("t"))  # dry-run sidecars use "t"
        if t is not None:
            out.append(round(float(t) - delay - clip["start_ts"], 2))
    return out


def handle_render(ctx: Context, p: dict) -> None:
    clip = _clip(ctx, p["clip_id"])
    if clip["status"] not in ("cut",):
        return
    src = _src(ctx, clip["source"])
    m = clip["meta"]
    raw, offset, duration = Path(m["raw"]), float(m["offset"]), float(m["duration"])
    if not raw.exists():
        raise PermanentError(f"source footage missing: {raw}")
    work = ctx.work_dir(clip["id"])

    # 1) transcribe just this clip with the subtitle model
    wav = media.extract_audio(raw, work / "audio.wav", offset, duration)
    tr = whisper.transcribe(ctx, wav, language=src.language)
    (work / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False))
    words = [Word(w["start"], w["end"], w["word"]) for s in tr["segments"] for w in s["words"]]

    # 2) subtitles + render
    ass = work / "subs.ass"
    ass.write_text(build_ass(words, ctx.cfg.g.output, src.layout))
    out = work / f"{clip['id']}.mp4"
    effects, zooms = motion.plan(wav, duration, ctx.cfg.g.output.motion, src.layout, ctx.cfg.g.output,
                                 chat_moments=_chat_moments(clip, offset))
    media.render(raw, out, start=offset, duration=duration, layout=src.layout, cfg=ctx.cfg.g.output, ass_path=ass,
                 threads=ctx.threads, fonts_dir=_fonts_dir(ctx), effects=effects)
    info = media.probe(out)
    real_len = info["duration"]

    # 3) campaigns: only those whose length rules the clip satisfies; suggestions for others
    tagged, rejected = [], {}
    for name in clip["campaigns"]:
        c = ctx.cfg.campaigns.get(name)
        if c is None:
            continue
        if real_len + 0.5 < c.min_seconds or real_len - 0.5 > c.max_seconds:
            rejected[name] = f"length {real_len:.1f}s outside {c.min_seconds}-{c.max_seconds}s"
        else:
            tagged.append(name)
    transcript_text = " ".join(s["text"] for s in tr["segments"])
    chat_text = " ".join(x["text"] for x in m.get("top_chat", []))
    suggested = captions.suggest_campaigns(ctx.cfg.campaigns, clip["campaigns"], transcript_text + " " + chat_text)

    # 4) captions
    quote = None
    if tr["language"] == "en":
        kws = src.vod.keywords
        quote = vtext.quotable_line(tr["segments"], 0, duration, kws)
    hook = m.get("hook")
    if not hook and ctx.cfg.g.llm.enabled:
        first = ctx.cfg.campaigns.get(tagged[0]) if tagged else None
        hook = llm_mod.hook(ctx.cfg.g.llm, transcript_text, first.creator_name if first else "")
    caps = {}
    for name in tagged:
        c = ctx.cfg.campaigns[name]
        text = captions.build_caption(clip["id"], c, hook=hook, quote=quote)
        problems = captions.validate(text, c)
        (work / f"{clip['id']}.{name}.txt").write_text(captions.caption_file_text(text, problems, c))
        caps[name] = {"text": text, "problems": problems}

    ctx.store.update_clip(clip["id"], status="rendered", campaigns=tagged, meta_update={
        "video": str(out), "duration": round(real_len, 2), "language": tr["language"], "quote": quote, "hook": hook,
        "captions": caps, "rejected_campaigns": rejected, "suggested_campaigns": suggested,
        "transcript_excerpt": transcript_text[:1500], "zooms": zooms})
    wav.unlink(missing_ok=True)
    if m.get("raw_is_temp"):
        raw.unlink(missing_ok=True)  # only our own cut piece, never a user/VOD file
    ctx.queue.enqueue("deliver", {"clip_id": clip["id"]}, priority=_prio(clip), dedupe_key=f"deliver:{clip['id']}")


def _fonts_dir(ctx: Context) -> str | None:
    d = Path(ctx.cfg.g.output.fonts_dir)
    if d.exists():
        return str(d)
    local = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    return str(local) if local.exists() else None


# ------------------------------------------------------------------- deliver


def sidecar(ctx: Context, clip: dict) -> dict:
    m = clip["meta"]
    tz = ZoneInfo(ctx.cfg.g.timezone)
    d = {
        "clip_id": clip["id"], "source": clip["source"], "mode": clip["mode"],
        "source_url": ctx.cfg.sources[clip["source"]].url if clip["source"] in ctx.cfg.sources else None,
        "score": round(clip["score"], 3), "rank_percentile": ctx.store.rank_percentile(clip["source"], clip["score"]),
        "campaigns": clip["campaigns"], "suggested_campaigns": m.get("suggested_campaigns", []),
        "rejected_campaigns": m.get("rejected_campaigns", {}),
        "duration": m.get("duration"), "language": m.get("language"), "hook": m.get("hook"),
        "captions": m.get("captions", {}),
        "created": datetime.fromtimestamp(clip["created_at"], tz).isoformat(timespec="seconds"),
    }
    if clip["mode"] == "live" and m.get("video_seconds"):  # dry run: positions are seconds in the input file
        d["position_in_video"] = {"start": clip["start_ts"], "end": clip["end_ts"]}
        d["spikes"] = m.get("spikes", [])
        d["top_chat"] = m.get("top_chat", [])
    elif clip["mode"] == "live":
        d["stream_time"] = {"start": datetime.fromtimestamp(clip["start_ts"], tz).isoformat(timespec="seconds"),
                            "end": datetime.fromtimestamp(clip["end_ts"], tz).isoformat(timespec="seconds")}
        d["spikes"] = m.get("spikes", [])
        d["top_chat"] = m.get("top_chat", [])
        d["gaps"] = m.get("gaps", [])
    else:
        d["video_title"] = m.get("title")
        d["video_url"] = m.get("item_url")
        d["position_in_video"] = {"start": clip["start_ts"], "end": clip["end_ts"]}
        d["highlight"] = m.get("highlight", {})
    d["transcript_excerpt"] = m.get("transcript_excerpt", "")
    return d


def handle_deliver(ctx: Context, p: dict) -> None:
    clip = _clip(ctx, p["clip_id"])
    if clip["status"] != "rendered":
        return
    m = clip["meta"]
    video = Path(m["video"])
    if not video.exists():
        raise PermanentError(f"rendered video missing: {video}")
    work = video.parent
    date = datetime.fromtimestamp(clip["created_at"], ZoneInfo(ctx.cfg.g.timezone)).strftime("%Y-%m-%d")
    side = work / f"{clip['id']}.json"
    side.write_text(json.dumps(sidecar(ctx, clip), ensure_ascii=False, indent=2))
    already = {d["campaign"]: d for d in ctx.store.deliveries(clip["id"])}
    targets = clip["campaigns"] or ["_untagged"]
    deliveries, first_remote = [], None
    for name in targets:
        if name in already:
            deliveries.append(already[name])
            continue
        cap = work / f"{clip['id']}.{name}.txt"
        if not cap.exists():
            cap.write_text("(no campaign: clip did not match any campaign rules)\n")
        files = {"video": video, "caption": cap, "sidecar": side}
        loc, link, rvideo = delivery.deliver(ctx.cfg.g.delivery, files, name, date, first_remote_video=first_remote)
        first_remote = first_remote or (rvideo if ctx.cfg.g.delivery.backend == "rclone" else None)
        ctx.store.add_delivery(clip["id"], name, loc, link)
        deliveries.append({"campaign": name, "location": loc, "link": link})
    ctx.store.update_clip(clip["id"], status="delivered", meta_update={
        "deliveries": [{"campaign": d["campaign"], "location": d["location"], "link": d.get("link")} for d in deliveries]})
    L.info("clip delivered", extra={"clip_id": clip["id"], "campaigns": targets, "score": clip["score"]})
    if telegram_videos(ctx):
        ctx.queue.enqueue("telegram", {"clip_id": clip["id"]}, priority=PRIORITY_LIVE, dedupe_key=f"telegram:{clip['id']}")
    elif ctx.cfg.g.delivery.delete_work_files:
        shutil.rmtree(work, ignore_errors=True)
    if clip["mode"] == "vod":
        _maybe_cleanup_vod(ctx, m.get("item_key"))


def telegram_videos(ctx: Context) -> bool:
    n = ctx.cfg.g.notifications
    return n.backend == "telegram" and n.telegram.send_video and not ctx.dry_run


def handle_telegram(ctx: Context, p: dict) -> None:
    """Send the finished clip + captions to Telegram (shrunk copy if over the upload limit)."""
    clip = _clip(ctx, p["clip_id"])
    if clip["notified"]:
        return
    n = ctx.cfg.g.notifications
    m = clip["meta"]
    work = Path(m["video"]).parent
    candidates = [Path(m["video"])] + [Path(d["location"]) / f"{clip['id']}.mp4" for d in m.get("deliveries", [])
                                       if ctx.cfg.g.delivery.backend == "local"]
    video = next((v for v in candidates if v.exists()), None)
    if video is None:
        raise PermanentError("no local copy of the clip to send")
    chat = notify.telegram_chat_id(n, ctx.store)
    if not chat:
        raise RuntimeError("telegram: no chat yet - open the bot in Telegram and press Start")
    tmp = work if work.exists() else ctx.work_dir(clip["id"])
    small = media.shrink_to_size(video, tmp / f"{clip['id']}.tg.mp4", n.telegram.max_upload_mb * 1_000_000,
                                 duration=float(m.get("duration") or 90), threads=ctx.threads)
    try:
        notify.send_clip_telegram(n, ctx.store, clip, small, chat)
    finally:
        if small != video:
            small.unlink(missing_ok=True)
    ctx.store.mark_notified([clip["id"]])
    L.info("clip sent to telegram", extra={"clip_id": clip["id"], "mb": round(small.stat().st_size / 1e6, 1) if small.exists() else None})
    if ctx.cfg.g.delivery.delete_work_files:
        shutil.rmtree(work, ignore_errors=True)


def _maybe_cleanup_vod(ctx: Context, item_key: str | None) -> None:
    if not item_key:
        return
    row = ctx.store.c.execute(
        "SELECT COUNT(*) FROM clips WHERE mode='vod' AND json_extract(meta,'$.item_key')=? AND status NOT IN ('delivered','failed')",
        (item_key,)).fetchone()
    if row[0] == 0:
        ctx.store.set_item_status(item_key, "done")
        src_name = ctx.store.c.execute("SELECT source FROM processed_items WHERE key=?", (item_key,)).fetchone()
        src = ctx.cfg.sources.get(src_name[0]) if src_name else None
        if src is not None and src.platform != "local" and not src.vod.keep_download:
            shutil.rmtree(ctx.vod_dir(item_key), ignore_errors=True)


# ----------------------------------------------------------------------- vod


def handle_ingest(ctx: Context, p: dict) -> None:
    src = _src(ctx, p["source"])
    video = ingest.download(p["url"], src, ctx.vod_dir(p["key"]), ctx.cfg.g.delivery.rclone)
    ctx.queue.enqueue("transcribe_full", p | {"video": str(video)}, priority=PRIORITY_VOD, dedupe_key=f"transcribe:{p['key']}")


def handle_transcribe_full(ctx: Context, p: dict) -> None:
    src = _src(ctx, p["source"])
    d = ctx.vod_dir(p["key"])
    out = d / "transcript.json"
    if not out.exists():
        wav = media.extract_audio(Path(p["video"]), d / "audio.wav")
        tr = whisper.transcribe(ctx, wav, language=src.language, model=ctx.cfg.g.whisper.vod_scoring_model)
        out.write_text(json.dumps(tr, ensure_ascii=False))
    ctx.queue.enqueue("analyze_vod", p, priority=PRIORITY_VOD, dedupe_key=f"analyze:{p['key']}")


def handle_analyze_vod(ctx: Context, p: dict) -> None:
    src = _src(ctx, p["source"])
    d = ctx.vod_dir(p["key"])
    video = Path(p["video"])
    tr = json.loads((d / "transcript.json").read_text())
    wav = d / "audio.wav"
    if not wav.exists():
        media.extract_audio(video, wav)
    campaigns = [ctx.cfg.campaigns[c] for c in src.campaigns]
    lo, hi = analyze.vod_length_limits(src, campaigns)
    feats = analyze.compute_features(wav, video, tr["segments"], src, ctx.threads)
    creator = campaigns[0].creator_name if campaigns else ""
    windows, score = analyze.select_windows(feats, tr["segments"], src, lo, hi, ctx.cfg.g.llm, creator)
    (d / "scores.csv").write_text("second,score\n" + "".join(f"{i},{v:.4f}\n" for i, v in enumerate(score)))
    L.info("vod analyzed", extra={"source": p["source"], "key": p["key"], "windows": len(windows), "length": len(score)})
    if not windows:
        ctx.store.set_item_status(p["key"], "done")
        return
    for rank, w in enumerate(windows, 1):
        cid = f"{p['source']}-{p['key'].split(':')[-1][:16]}-{int(w.start):05d}"
        meta = {"raw": str(video), "offset": w.start, "duration": round(w.length, 3), "item_key": p["key"],
                "item_url": p["url"], "title": p.get("title", ""), "hook": w.hook,
                "highlight": w.detail | {"rank_in_video": rank}}
        ctx.store.add_clip(cid, p["source"], "vod", w.start, w.end, round(w.score, 3), list(src.campaigns), meta, status="cut")
        ctx.queue.enqueue("render", {"clip_id": cid}, priority=PRIORITY_VOD, dedupe_key=f"render:{cid}")
    wav.unlink(missing_ok=True)


HANDLERS = {
    "cut": handle_cut,
    "render": handle_render,
    "deliver": handle_deliver,
    "ingest": handle_ingest,
    "transcribe_full": handle_transcribe_full,
    "analyze_vod": handle_analyze_vod,
    "telegram": handle_telegram,
}

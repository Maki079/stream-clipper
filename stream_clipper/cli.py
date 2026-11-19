"""Command line entry point: stream-clipper <command> ..."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from . import log
from .config import ConfigError, load_config

DEFAULT_CONFIG = os.environ.get("CLIPPER_CONFIG", "/config/config.yaml")


def cmd_validate(args) -> int:
    cfg = load_config(args.config)
    print(f"OK: {len(cfg.campaigns)} campaigns, {len(cfg.sources)} sources "
          f"({len(cfg.enabled_sources('live'))} live, {len(cfg.enabled_sources('vod'))} vod enabled)")
    for w in cfg.warnings():
        print(f"WARNING: {w}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    log.setup_logging(cfg.g.log_level)
    from .supervisor import run

    run(args.config, cfg, record_chat=args.record_chat)
    return 0


def cmd_health(args) -> int:
    from .supervisor import health

    ok, msg = health(load_config(args.config))
    print(msg)
    return 0 if ok else 1


def cmd_dry_run(args) -> int:
    cfg = load_config(args.config)
    log.setup_logging(cfg.g.log_level, json_logs=False)
    from .dryrun import run

    simulate = [float(x) for x in args.simulate.split(",")] if args.simulate else None
    clips = run(cfg, args.source, Path(args.video), outdir=Path(args.out), chat=Path(args.chat) if args.chat else None,
                chat_offset=args.chat_offset, simulate=simulate, vod=args.vod, threads=args.threads, warmup=args.warmup)
    print(f"\n{len(clips)} clip(s):")
    for c in clips:
        m = c["meta"]
        print(f"  [{c['status']}] {c['id']} score={c['score']:.2f} {c['start_ts']:.1f}-{c['end_ts']:.1f}s "
              f"campaigns={c['campaigns']} {m.get('rejected_campaigns') or ''}")
        for d in m.get("deliveries", []):
            print(f"      -> {d['link']}")
    print(f"\nOutput: {Path(args.out) / 'output'}")
    return 0 if all(c["status"] == "delivered" for c in clips) else 1


def cmd_crop(args) -> int:
    from .crophelper import main

    return main(args.input, at=args.at, outdir=Path(args.out), facecam=args.facecam, gameplay=args.gameplay,
                main_rect=args.main, facecam_height=args.facecam_height)


def cmd_record_chat(args) -> int:
    """Write a source's live chat to jsonl (for tuning spike settings / dry runs)."""
    cfg = load_config(args.config)
    log.setup_logging(cfg.g.log_level, json_logs=False)
    from .live.chat import make_chat

    src = cfg.sources[args.source]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    chat = make_chat(src)
    if chat is None:
        print("source has no chat backend", file=sys.stderr)
        return 1

    async def go():
        n = 0
        deadline = time.time() + args.minutes * 60
        with out.open("a") as f:
            async for m in chat.messages():
                f.write(json.dumps({"ts": m.ts, "user": m.user, "text": m.text}, ensure_ascii=False) + "\n")
                n += 1
                if n % 500 == 0:
                    print(f"{n} messages", file=sys.stderr)
                if time.time() > deadline:
                    break
        await chat.close()
        print(f"wrote {n} messages to {out}")

    asyncio.run(go())
    return 0


def cmd_replay_chat(args) -> int:
    """Replay a recorded chat log through the spike detector with the source's settings."""
    cfg = load_config(args.config)
    from .live.spikes import ChatMessage, SpikeDetector

    src = cfg.sources[args.source]
    rows = [json.loads(line) for line in Path(args.chat).read_text().splitlines() if line.strip()]
    if not rows:
        print("empty chat log")
        return 1
    t0 = rows[0].get("ts", 0)
    det = SpikeDetector(src.spike, t0, max_spike_seconds=src.clip.max_seconds)
    i, end = 0, rows[-1].get("ts", rows[-1].get("t", 0))
    t = t0
    peaks = []
    print(f"{len(rows)} messages over {(end - t0) / 60:.1f} min")
    while t <= end + 30:
        t += 1
        while i < len(rows) and rows[i].get("ts", rows[i].get("t")) <= t:
            det.add(ChatMessage(rows[i].get("ts", rows[i].get("t")), rows[i]["user"], rows[i]["text"]))
            i += 1
        msgs, uniq, _ = det._measure(t)
        peaks.append(msgs)
        if (s := det.tick(t)) is not None:
            print(f"SPIKE at +{s.start - t0:7.0f}s  len {s.end - s.start:3.0f}s  peak {s.peak_messages} msgs/"
                  f"{src.spike.window_seconds}s (unique {s.peak_unique})  baseline {s.baseline_messages:.1f}  z {s.peak_z:.1f}")
    if peaks:
        ps = sorted(peaks)
        print(f"messages per {src.spike.window_seconds}s window: median {ps[len(ps) // 2]}, p95 {ps[int(len(ps) * .95)]}, "
              f"max {ps[-1]}  (current min_messages={src.spike.min_messages})")
    return 0


def cmd_telegram_test(args) -> int:
    """Find the chat that pressed Start on the bot, remember it, send a test message."""
    cfg = load_config(args.config)
    n = cfg.g.notifications
    if n.backend != "telegram":
        print("notifications.backend is not telegram")
        return 1
    from . import notify
    from .context import Context

    ctx = Context.create(cfg, name="telegram-test")
    me = notify.tg_call(n, "getMe")
    chat = notify.telegram_chat_id(n, ctx.store)
    if not chat:
        print(f"Bot @{me['username']} works, but nobody wrote to it yet: open https://t.me/{me['username']}, "
              "press Start, then run this again.")
        return 1
    ok = notify.send(n, "stream-clipper connected ✅", "Clips will arrive here as videos with ready-to-paste captions.",
                     ctx.store)
    print(f"bot @{me['username']} -> chat {chat}: {'sent' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_queue(args) -> int:
    from .context import Context

    ctx = Context.create(load_config(args.config), name="cli")
    if args.action == "stats":
        for t, st in sorted(ctx.queue.counts().items()):
            print(f"{t:16s} " + "  ".join(f"{k}={v}" for k, v in sorted(st.items())))
    elif args.action == "ls":
        for j in ctx.queue.list(args.status, args.limit):
            err = (j["error"] or "").strip().splitlines()
            print(f"#{j['id']:<6} {j['type']:16s} {j['status']:8s} attempts={j['attempts']} {j['payload'][:80]}"
                  + (f"\n        error: {err[0][:150]}" if err else ""))
    elif args.action == "retry":
        print(f"requeued {ctx.queue.retry_failed(args.id)} job(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stream-clipper", description="Auto-clipper for live streams and VODs")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("web", help="open the web interface for configuration and service management")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--autostart", action="store_true", help="start clipping if a configuration already exists")
    s.add_argument("--record-chat", action="store_true", help="save live chat while the managed service runs")
    def cmd_web(args):
        from .web import serve

        return serve(args)
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("run", help="run the service (live recorders, VOD watcher, workers)")
    s.add_argument("--record-chat", action="store_true", help="also save all chat to data/chatlogs/<source>.jsonl")
    s.set_defaults(fn=cmd_run)

    sub.add_parser("validate-config", help="validate config.yaml and print warnings").set_defaults(fn=cmd_validate)
    sub.add_parser("health", help="exit 0 if the service is healthy (docker healthcheck)").set_defaults(fn=cmd_health)

    s = sub.add_parser("dry-run", help="full pipeline on a local video, output to a local folder, no upload")
    s.add_argument("--source", required=True, help="source name from config (layout, language, campaigns)")
    s.add_argument("--video", required=True)
    s.add_argument("--chat", help="chat jsonl ({'t': sec, 'user', 'text'} or record-chat output)")
    s.add_argument("--chat-offset", type=float, help="epoch of video start when the chat log has absolute 'ts'")
    s.add_argument("--simulate", help="simulate chat with bursts at these video seconds, e.g. 120,400")
    s.add_argument("--vod", action="store_true", help="use VOD highlight scoring instead of chat")
    s.add_argument("--warmup", type=float, help="override spike warm-up seconds")
    s.add_argument("--threads", type=int, default=8)
    s.add_argument("--out", default="./dryrun")
    s.set_defaults(fn=cmd_dry_run)

    s = sub.add_parser("crop-helper", help="grab a frame, draw crop rectangles, preview the vertical layout")
    s.add_argument("input", help="stream URL, video URL or local file")
    s.add_argument("--at", type=float, default=0, help="seconds into the video (files/VODs)")
    s.add_argument("--facecam", help="x,y,w,h")
    s.add_argument("--gameplay", help="x,y,w,h")
    s.add_argument("--main", help="x,y,w,h (center_crop)")
    s.add_argument("--facecam-height", type=int, default=700)
    s.add_argument("--out", default="./crop")
    s.set_defaults(fn=cmd_crop)

    s = sub.add_parser("record-chat", help="save a live chat to jsonl for tuning")
    s.add_argument("--source", required=True)
    s.add_argument("--minutes", type=float, default=60)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_record_chat)

    s = sub.add_parser("replay-chat", help="replay a chat jsonl through the spike detector (tuning)")
    s.add_argument("--source", required=True)
    s.add_argument("--chat", required=True)
    s.set_defaults(fn=cmd_replay_chat)

    sub.add_parser("telegram-test", help="connect the Telegram bot (after pressing Start) and send a test message"
                   ).set_defaults(fn=cmd_telegram_test)

    s = sub.add_parser("queue", help="inspect / retry jobs")
    s.add_argument("action", choices=["stats", "ls", "retry"])
    s.add_argument("--status", choices=["queued", "running", "done", "failed"])
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--id", type=int, help="retry only this job")
    s.set_defaults(fn=cmd_queue)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

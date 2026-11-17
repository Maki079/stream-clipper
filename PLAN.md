# stream-clipper — Implementation Plan

Status: **APPROVED, in progress.** Decisions made on 2026-11-17 (the owner said "decide yourself"):

| # | Decision |
|---|---|
| Q1 | This box (16 vCPU / 62 GB, Ubuntu 24.04) is production. Docker 29, compose v2 and ffmpeg 6.1 have been installed. Defaults are tuned for 16 vCPU: **3 CPU workers × 4 threads**. |
| Q2 | Full VOD scoring uses Whisper `small`. The chosen windows are re-transcribed with `medium` for subtitles. Both are configurable. |
| Q3 | Clips are uploaded only to campaigns listed on the source. Other campaigns whose `creator_aliases` appear in the transcript/chat are listed as `suggested_campaigns` in the sidecar. They are not uploaded. |
| Q4 | One caption per campaign. |
| Q5 | **Delivery is local for now**: `delivery.backend: local` writes to `/output/<campaign>/<date>/`. The `rclone` backend is implemented and can be switched on later (private Drive file links). |
| Q6 | Telegram and ntfy are both supported. Off until configured. Daily summary at 20:00 Europe/Bratislava. |
| Q7 | Kick ships as experimental (yt-dlp + curl_cffi, no browser). |
| Q8 | My own innertube poller is the default YouTube chat source. chat-downloader / Data API v3 are alternatives. |
| Q9 | Subtitles are in the original language. The hook is always English. The transcript quote is only used when the language is English. |
| Q10 | One clip at the longest length any tagged campaign requires. |

## 1. Goal

A headless, Dockerized service that watches live streams and finished videos, finds
highlights, renders vertical 1080x1920 clips with burned-in TikTok-style subtitles,
writes captions that follow each campaign's rules, and uploads everything to Google
Drive. You only post the clips.

Everything uses free, open-source tools. No paid APIs.

---

## 2. Findings before planning

### 2.1 Kick support (you asked me to check)

I checked the current releases on PyPI (streamlink 8.6.3, yt-dlp 2026.10.26):

| Tool | Kick support | Catch |
|---|---|---|
| streamlink | `kick` plugin (live + VOD) | Needs a **real web browser** (Chromium) to solve Kick's JS challenge. That means headless Chromium in the container: about +400 MB and fragile. |
| yt-dlp | `kick:live`, `kick:vod`, `kick:clip`, `kick:videos` extractors | Uses `curl_cffi` browser impersonation to get past Cloudflare. Light, but breaks every time Kick changes its protection. |
| Chat | No official free API. The public Pusher websocket (read-only, no login) is what the website uses. | Unofficial. The chatroom ID lookup goes through the same Cloudflare-protected API. |

**Recommendation:** treat Kick as **experimental**. Use yt-dlp with `curl_cffi` to resolve
the HLS URL, then record that HLS URL with the same buffer pipeline. Re-resolve on every
reconnect. Use a Pusher websocket chat client. Do not use Chromium. Twitch and YouTube are
the supported platforms. Kick works when Cloudflare allows it.

### 2.2 YouTube live chat libraries are unmaintained

- `pytchat`: last release 2021-07.
- `chat-downloader`: last release 2023-09.

Both scrape YouTube's internal "innertube" live-chat endpoint and break when YouTube
changes it. **Recommendation:** write a small innertube poller of my own (about 150 lines,
`httpx` only) as the default `YouTubeChatSource`. Keep `chat-downloader` as an optional
fallback and the official Data API v3 (`liveChatMessages`, free quota, needs an API key)
as a third option, all behind the same `ChatSource` interface.

### 2.3 This machine is not the spec machine

The dev box I'm running on has **16 vCPU, 62 GB RAM, Python 3.12.3, and no Docker,
ffmpeg, streamlink, yt-dlp or rclone**. Your spec says 32 vCPU. See open question Q1.

---

## 3. Architecture

One container and one Python package (`stream_clipper`). One long-running process
(`stream-clipper run`) runs:

```
                    ┌────────────────── supervisor (asyncio) ───────────────────┐
 LIVE source x N ─▶ │ Recorder ──▶ rolling segment buffer (/buffer/<src>/*.ts)  │
                    │ ChatSource ─▶ SpikeDetector ─▶ ClipWindow ─▶ enqueue CUT  │
 VOD source/watch ─▶│ VodWatcher (poll channel/playlist/Drive/local) ─▶ enqueue │
                    │ Notifier (batches + daily summary) · Health heartbeat     │
                    └──────────────────────────────┬────────────────────────────┘
                                                   ▼
                                   SQLite job queue (data/state.db, WAL)
                                                   ▼
          ┌───────────── CPU pool (N processes, thread-limited) ─────────────┐
          │ transcribe · analyze_vod · render       (Whisper loaded once)     │
          └──────────────────────────────────────────────────────────────────┘
          ┌───────────── IO lane (async, a few slots) ───────────────────────┐
          │ download · cut · upload · notify                                  │
          └──────────────────────────────────────────────────────────────────┘
```

I split workers into two lanes so slow uploads and downloads never hold a CPU worker. Both
lanes read from the same queue table and filter by job type.

### 3.1 Job pipeline

**LIVE:** `cut` (IO) → `render` (CPU: transcribe clip with medium + ASS + encode + captions) → `deliver` (IO)

**VOD:** `ingest` (IO) → `transcribe_full` (CPU, small) → `analyze_vod` (CPU: score + pick windows)
→ for each window: `render` (same job as live: medium re-transcription of just that window) → `deliver`

Notifications are sent by the supervisor (batched), not as jobs. *(As built; this replaced the
earlier separate transcribe_clip/caption/upload/notify job types to keep the Whisper model and
the data in one worker.)*

Each job is idempotent and writes its outputs to `/data/work/<clip_id>/`. A crash or
restart re-runs only the unfinished job.

---

## 4. Modules

```
stream_clipper/
  cli.py              run | dry-run | crop-helper | record-chat | queue (ls/retry) | health | validate-config
  config.py           pydantic models: GlobalCfg, SourceCfg, CampaignCfg, cross-reference checks
  log.py              JSON logging (stdlib, no extra dependency)
  db.py               SQLite schema + migrations: jobs, clips, processed_items, uploads, notifications
  queue.py            enqueue / claim (atomic UPDATE…RETURNING) / heartbeat / retry with backoff / requeue stale
  workers.py          process pool, per-worker thread limits (OMP/MKL/CT2 env), job dispatch
  supervisor.py       starts/stops source tasks, signal handling, graceful shutdown
  live/
    recorder.py       streamlink → ffmpeg segment muxer, 5 s segments, retention, reconnect/poll
    resolve.py        URL resolution: Twitch, YouTube /live (re-resolve), Kick (yt-dlp+curl_cffi)
    buffer.py         segment index (wall-clock ↔ file), pinning, selecting segments for a time range
    chat/base.py      ChatSource interface, ChatMessage(ts, user, text), backoff wrapper
    chat/twitch.py    anonymous IRC (justinfan<random>), raw asyncio socket, no library
    chat/youtube.py   innertube poller (default) | chat-downloader | Data API v3
    chat/kick.py      Pusher websocket (experimental)
    spikes.py         pure logic: windows, baseline, z-score, warm-up, cooldown, merge
    window.py         pure logic: spike → [start, end] with pre/post, delay offset, campaign limits
  vod/
    ingest.py         yt-dlp / rclone copy / local; processed_items dedupe (by URL id + file hash)
    watcher.py        poll channel/playlist (yt-dlp --flat-playlist), Drive folder, local folder
    audio.py          decode mono 16 kHz with ffmpeg → numpy; RMS/loudness, bursts
    scenes.py         ffmpeg scene detection → scene-change density
    text.py           transcript cues: speech rate, !, ?, keywords
    llm.py            optional Ollama HTTP client (off by default)
    scoring.py        combine features → per-second score → top-N non-overlapping windows
  media/
    whisper.py        faster-whisper wrapper, model singleton per worker process
    ass.py            word timestamps → TikTok-style ASS (3-5 words, max 2 lines, safe zones)
    layouts.py        ffmpeg filter graphs: stack, center_crop, blur_fit
    render.py         cut/concat + render command, ffprobe checks
  captions.py         per-campaign caption text, rule validation (hashtags, mention, length)
  ranking.py          normalized clip score across live/VOD
  upload.py           rclone copyto + server-side copy for extra campaigns, file-ID links
  notify.py           Telegram | ntfy backends, batching, daily summary
tools/
  simulate_chat.py    generate a synthetic chat log with spikes, for dry-run and tests
tests/                pytest; ffmpeg-dependent tests are marked and skipped if ffmpeg is missing
```

---

## 5. Key design decisions

### 5.1 Live recording and the buffer
- `streamlink --stdout <url> best` is piped into
  `ffmpeg -c copy -f segment -segment_time 5 -reset_timestamps 1 -strftime 1 %s.ts`.
  Each segment's filename is the **wall-clock epoch when it started**, which gives a direct
  time-to-file mapping with no extra bookkeeping.
- Twitch: `--twitch-disable-ads`. Removed ad segments leave a gap in wall-clock time.
  Gaps are fine because we select by wall clock and re-encode when rendering.
- Retention: delete segments older than `buffer_seconds` (default 300), **except pinned
  segments**. A pending clip pins its segments until its `cut` job finishes. Without
  pinning, a slow queue could lose footage.
- Offline: poll every `poll_interval` (default 60 s), with exponential backoff on errors.
  YouTube `/channel/live` and Kick are re-resolved on every reconnect.
- Segments are split at keyframes, so they are about 5 s, not exactly 5 s. Precise trimming
  happens in the render step (frame-accurate, because we re-encode anyway).

### 5.2 Aligning chat with video
Both the video segments and the chat messages are timestamped with **our server's clock
at the moment we receive them**. The recorder and the chat client are on the same viewer
path, so their delivery latencies are similar. What's left is the time chatters take to
react and type. A per-source `chat_delay_seconds` (default 6) is subtracted from the spike
time:

`video_time_of_moment ≈ spike_time - chat_delay_seconds`

The 30 s pre-roll easily absorbs the remaining error. The README explains how to calibrate
it: in dry-run, compare a known moment with its chat spike. Twitch's `tmi-sent-ts` is
logged but not used, because it comes from a different clock.

### 5.3 Spike detection (`spikes.py`, pure and fully unit-tested)
- Every 1 s, compute a sliding 10 s window: `messages`, `unique_chatters`. Messages matching
  `ignore_patterns` and known bots are filtered out first. Repeated identical messages from
  the same user count once.
- Baseline: mean/std over the last 300 s of window values. Windows inside recent spikes
  are excluded. The std has a floor so a quiet chat doesn't produce huge z-scores.
- Trigger when `(value > multiplier*baseline OR z > z_threshold) AND messages >= min_messages`.
  Both metrics are evaluated, and triggering on either one is configurable.
- `warmup_seconds` (default 180): no triggers before then.
- `cooldown_seconds` (default 90). A spike inside an open clip's window extends that clip,
  up to `max_clip_seconds` (120).
- Spike strength = peak z-score. This is used for ranking.
- Very large chats: `min_messages` is per source. The README gives sizing guidance and the
  `record-chat` command lets you capture real chat to tune against.

### 5.4 Clip window (`window.py`)
`start = spike_start - delay - pre`, `end = spike_end - delay + post`, then clamp the
length to `[campaign.min, max_clip]`. If the clip is shorter than the strictest tagged
campaign's minimum (for example 61 s), the post window is extended. The cut job is scheduled
for `end + one segment + safety margin`.

### 5.5 VOD highlight scoring
All features are computed on a 1 s grid, robust-z-normalized (median/MAD) per video, then
combined with weights from the config:

| Feature | Method |
|---|---|
| audio_energy | RMS in dB vs a rolling 60 s median |
| shouting | sustained high-loudness segments that are speech according to VAD |
| laughter_like | short rhythmic energy bursts (heuristic, weak signal, low default weight) |
| speech_density | words/sec from the Whisper word timestamps |
| excitement | `!`, `?`, ALL-CAPS, configurable keyword list |
| scene_density | ffmpeg `select='gt(scene,0.3)'` cuts per window |
| llm (optional) | Ollama rates only the top-K candidate windows (CPU 7B is slow), returns score + hook title |

Window selection: sliding windows of every length in `[min,max]` (5 s step). Take the
highest-scoring window, remove overlapping candidates, repeat until N windows are picked.
Then snap start and end to Whisper segment (sentence) boundaries without leaving the
length limits. `stream-clipper dry-run --vod --report` writes a per-second score CSV so
you can tune the weights.

### 5.6 Rendering
- One ffmpeg command per clip: trim → layout filter graph → `ass=` → libx264 `medium`,
  `-b:v 8M -maxrate 10M -bufsize 16M`, `yuv420p`, AAC 160k 48 kHz, `+faststart`.
  Each worker gets `-threads` set to its thread budget.
- Layouts:
  - `stack`: facecam crop scaled to 1080 × `facecam_height` (700). The gameplay crop is
    scaled to cover 1080 × 1220 and center-cropped. The two are stacked vertically.
  - `center_crop`: one crop rectangle scaled and cropped to cover 1080x1920.
  - `blur_fit`: background = scaled to cover + `boxblur` + slight zoom. Foreground = full
    frame at 1080 wide, centered.
- Subtitle safe zones per layout. `stack`: just below the facecam/gameplay seam, never
  over the facecam. `center_crop`: around 62% height. `blur_fit`: in the band below the
  16:9 frame. Every layout can override `subtitle_y`. All layouts avoid the bottom 20%,
  where TikTok/Reels draw their own UI.
- ASS style: a bold free font (**Montserrat ExtraBold**, OFL license, with Noto Sans for
  non-Latin text), size around 80, thick black outline. 3-5 words per event, broken at
  pauses longer than 0.4 s or at punctuation, max 2 lines.
- No logos, watermarks or branding are added.

### 5.7 Whisper
faster-whisper `medium`, `int8`, CPU, `vad_filter=True`, `word_timestamps=True`, language
per source (`auto` by default). The model is loaded **once per CPU worker process** and
`cpu_threads` = the worker's thread budget. Cached in the `/models` volume.

### 5.8 Captions and campaigns
- A clip is tagged with the source's `campaigns`, filtered by: platform allowed, clip length
  inside the campaign range. Optionally, also tag campaigns whose `creator_aliases` appear in
  the transcript or chat (for clips with two creators). See Q3.
- Caption = hook line + creator mention + required hashtags (+ optional extra tags).
  Validated against the campaign rules. If a required element is missing, that is an error
  and the clip is not uploaded for that campaign.
- Hook: Ollama if enabled. Otherwise an English template (`"{creator} did NOT expect this 😭"`
  and similar, configurable list) plus the most quotable transcript line (short, high
  excitement score, chosen by heuristic).

### 5.9 Upload
- `rclone copyto` into `<remote>:<root>/<campaign>/<YYYY-MM-DD>/<clip_id>.{mp4,txt,json}`.
  Each extra campaign gets a server-side `rclone copyto` (no re-upload) with its own caption.
- Drive link: the file ID from `rclone lsjson` → `https://drive.google.com/file/d/<id>/view`.
  This link is **private** (only your account can open it). I deliberately avoid
  `rclone link`, which makes files public. See Q5.
- Retry with exponential backoff (max attempts configurable). Local files are deleted
  only after every campaign upload has succeeded. Clips that keep failing stay in
  `/data/failed/` and appear in the daily summary.

### 5.10 Queue and performance (target: 32 vCPU / 64 GB)
- SQLite in WAL mode, `jobs(id, type, payload, status, priority, attempts, run_after,
  worker, heartbeat_at, error, created_at, updated_at)`. At startup, `running` jobs with a
  stale heartbeat go back to `queued`. Priority: live > VOD.
- Defaults: **3 CPU workers × 8 threads** = 24 threads. That leaves about 8 vCPU for 5–10
  recorders (stream copy, about 1–3% CPU each), chat, and the IO lane.
  RAM: Whisper medium int8 uses about 1.5–2.5 GB per worker.
- Rough estimates, to be measured in stage 2: a 65 s clip takes about 30–60 s to
  transcribe and 30–60 s to render with 8 threads. That's roughly 1.5–2 min of worker time
  per clip, or **about 90–120 live clips/hour** across 3 workers. That covers 5 channels
  even during busy periods.
- **Full VOD transcription is the bottleneck**: medium on 8 CPU threads runs at very
  roughly 1x realtime, so a 3 h VOD takes about 3 h of one worker. See Q2.

### 5.11 Ops
- Dockerfile: `python:3.12-slim` + Debian `ffmpeg` + `rclone` (official static binary) +
  `fonts-noto-core` + Montserrat (from the Google Fonts GitHub repository) + pip (streamlink,
  yt-dlp[curl-cffi], faster-whisper, …). Non-root user. `tini` as PID 1.
- docker-compose: volumes `./config`, `./buffer`, `./models`, `./rclone`, `./data`,
  `./output`. `restart: unless-stopped`, `stop_grace_period: 60s`. The healthcheck runs
  `stream-clipper health`: supervisor heartbeat < 60 s old and DB reachable.
- Graceful shutdown on SIGTERM: stop accepting new spikes, flush clip windows that are
  already open (cut what is available), let running jobs finish up to the grace period,
  then requeue the rest.
- JSON log lines with `source`, `job_id`, `clip_id` fields.

### 5.12 Dependencies (all free/OSS)
streamlink (BSD), yt-dlp (Unlicense), curl_cffi (MIT), faster-whisper/ctranslate2 (MIT),
numpy (BSD), pydantic (MIT), PyYAML (MIT), httpx (BSD), websockets (BSD), Pillow (HPND,
crop helper), pytest (MIT); optional: chat-downloader (MIT), google-api-python-client
(Apache-2.0). External binaries: ffmpeg (LGPL/GPL), rclone (MIT), Ollama (MIT, optional,
separate compose service under a profile). Notifications: Telegram Bot API or ntfy.sh
(both free).

---

## 6. Build stages (tests run after each)

| # | Stage | Tests |
|---|---|---|
| 0 | Skeleton, pyproject, config models + example `config.yaml`, logging, CLI `validate-config` | config validation (valid example, bad refs, bad ranges, clear messages) |
| 1 | SQLite DB, queue, worker pool, retries, restart recovery | claim atomicity, backoff, stale-job recovery |
| 2 | Media: whisper wrapper, ASS generator, layouts, render, crop-helper | ASS chunking/lines/positions; ffmpeg graph strings; render a synthetic lavfi clip and check 1080x1920/H.264/AAC/yuv420p with ffprobe |
| 3 | Live: spikes, window math, buffer/pinning, recorder, Twitch/YouTube/Kick chat | spike detection on synthetic series (warm-up, cooldown, merge, ignore patterns, big-chat minimum); window math; buffer segment selection |
| 4 | VOD: ingest + dedupe, watcher, audio/scene/text features, scoring, selection, Ollama client (mocked) | scoring on a synthetic audio track with planted loud bursts; non-overlap + length limits + sentence snapping |
| 5 | Captions, ranking, rclone upload, notifications, daily summary | caption rules per campaign; upload command building (rclone mocked); notifier payloads |
| 6 | Dry-run end to end (local video + simulated chat), Dockerfile, compose, README | dry-run integration test producing a clip, caption and sidecar locally |

---

## 7. Risks

1. **Rights and platform rules.** Clipping other creators without permission risks
   strikes. Campaign rules must be followed. The README will say this prominently. The app
   enforces only what is in the config.
2. **Unofficial endpoints break.** YouTube innertube chat, Kick (Cloudflare), and sometimes
   streamlink/yt-dlp after site changes. Mitigation: everything is behind interfaces,
   chat failure never stops recording, and the image can be rebuilt to update
   yt-dlp/streamlink.
3. **CPU budget for VOD transcription** (see 5.10 / Q2).
4. **Heuristic VOD scoring is noisy.** Without ML models, "laughter" and "excitement" are
   rough signals. Expect to tune weights. The optional LLM helps but is slow on CPU.
5. **Spike false positives** (raids, sub trains, bot floods, emote walls). Mitigation:
   unique-chatter metric, ignore_patterns, dedupe of repeated messages, cooldown.
6. **Twitch ads / discontinuities** in stream-copied segments. Mitigation: wall-clock
   selection plus a full re-encode at render.
7. **Whisper hallucinations** on music or silence. Mitigation: VAD, plus dropping segments
   with low avg_logprob or a high no_speech_prob.
8. **Google Drive limits** (750 GB/day upload, API rate limits). Far above our volume.
   rclone handles the rate limits.
9. **Disk**: 5 channels × 5 min × ~8 Mbps ≈ 1.5 GB of buffer. VOD downloads are deleted
   after processing (configurable).

---

## 8. Open questions (please answer before I start)

**Q1. Dev/test environment.** This box has 16 vCPU, no Docker and no ffmpeg. Is it the
production server? May I (a) install `ffmpeg` with apt (needs sudo, which you'd run), or
(b) download a static ffmpeg build into the project for local tests? I can write the
Dockerfile/compose but **cannot build or test the image here** unless Docker is installed.

**Q2. VOD transcription model.** Full-length `medium` on CPU ≈ 1x realtime per worker. Options:
  - (a) Keep `medium` for everything (your spec). Slow on long VODs.
  - (b) **Recommended:** `small` for full-VOD scoring (about 3–4× faster), then re-transcribe
    only the chosen windows with `medium` for the burned-in subtitles. Same subtitle quality.

**Q3. Multi-creator tagging.** Should a clip be tagged only with the campaigns listed on
its source, or also auto-tagged when another campaign's `creator_aliases` (e.g. "jason",
"jasontheween") show up in the transcript or chat? Auto-tagging would add those campaigns
as suggestions in the JSON sidecar, not upload to them. Or upload to them too?

**Q4. Captions per platform.** One caption per campaign, or one per allowed platform
(TikTok / Reels / Shorts have different hashtag habits and length limits)?

**Q5. Drive links in notifications.** Private file links (recommended, only you can open
them), or public "anyone with link" links?

**Q6. Notifications.** Telegram, ntfy, or both available? Daily summary time and timezone?

**Q7. Kick.** OK to ship Kick as "experimental" (yt-dlp + curl_cffi, no browser), as
described in 2.1?

**Q8. YouTube chat.** OK to use my own innertube poller as the default (unofficial; same
approach as pytchat/chat-downloader), with chat-downloader and the official API as
alternatives?

**Q9. Language.** For non-English streams: subtitles in the original language (Whisper
`transcribe`), and the hook line always English. Should the "quotable transcript line" in
the caption also be translated to English (Whisper `translate` task, extra CPU) or left
out for non-English clips?

**Q10. Live clip length vs campaign.** If one stream feeds campaigns with different
minimum lengths, should we render **one clip at the longest required length** (simplest,
recommended) or separate cuts per campaign?

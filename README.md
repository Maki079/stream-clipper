# stream-clipper

Headless, Dockerized auto-clipper for paid clipping campaigns.

- **LIVE**: records Twitch / YouTube (and, experimentally, Kick) into a rolling 5-minute buffer, watches chat,
  and cuts a clip around every chat spike.
- **VOD**: downloads finished videos (YouTube, Twitch VODs, anything yt-dlp supports, Google Drive, local files),
  transcribes them and picks the best moments from audio, transcript and scene changes (optionally rated by a
  local LLM).
- Every clip is rendered vertical 1080x1920 with burned-in TikTok-style subtitles, gets a ready-to-paste caption
  per campaign plus a JSON sidecar, and is delivered to a local folder (default) or Google Drive.
- Optional Telegram / ntfy notifications for new clips and a daily summary.

Your only manual job is posting.

> ⚠️ **Rules and rights.** Each campaign has its own rules (hashtags, mentions, length, platforms, no logos …).
> The app only enforces what is in `config.yaml`; read every brief and keep the config in sync. Captions that break
> a rule are marked `!!! DO NOT POST AS IS` at the top of the `.txt`. Posting other creators' content without
> their permission (or outside an official campaign) can get your accounts struck or banned.

---

## 1. Quick start (this server)

```bash
cd /home/mroz/clipper
cp config/config.example.yaml config/config.yaml   # already done once
nano config/config.yaml                             # fill FILL_ME values from the campaign briefs
docker compose run --rm clipper validate-config     # must print OK
docker compose up -d --build                        # start (restarts automatically)
docker compose logs -f                              # watch
```

Clips appear in `./output/<campaign>/<YYYY-MM-DD>/`:

| file | what |
|---|---|
| `<clip>.mp4` | 1080x1920 H.264/AAC, ~8 Mbps, faststart, subtitles burned in |
| `<clip>.<campaign>.txt` | caption to paste (hook line, quote, creator mention, hashtags) + campaign notes |
| `<clip>.json` | sidecar: source, time, score, rank, campaigns, spikes + top chat lines or transcript excerpt |

The same clip appears in several campaign folders if it is tagged for several campaigns (hard links: no extra disk).

Stop: `docker compose down` (running renders finish; everything else is requeued and resumes on next start).

---

## 2. How it works

```
LIVE: streamlink → ffmpeg 5 s segments (stream copy) → rolling buffer ─┐
      chat (IRC / innertube / pusher) → spike detector → clip window ──┴→ cut → render → deliver → notify
VOD : watch list / URL / Drive / local → download → transcribe (small) → score → top N windows → render → deliver
render = transcribe the clip (medium) → ASS subtitles → layout + burn-in (x264 medium)
```

All work goes through a SQLite job queue (`data/state.db`) that survives restarts. CPU-heavy jobs
(transcribe, analyze, render) run in `cpu_workers` processes with `threads_per_worker` threads each; downloads, cuts
and deliveries run in separate IO slots so they never block a CPU worker.

### Chat ↔ video alignment
Video segments and chat messages are both stamped with **our server clock at the moment we receive them**. The
recorder and the chat client are both "viewers", so their delivery delays are similar. What remains is chat's
reaction time: people type a few seconds after the moment. `clip.chat_delay_seconds` (default 6) is subtracted from
the spike time. The 30 s pre-roll absorbs any remaining error. To calibrate: run a dry run on a recorded stream and
compare a known moment with its spike (see §7).

### Live clip window
`start = spike − chat_delay − pre_seconds (30)`, `end = spike − chat_delay + post_seconds (35)` → 65 s by default
(TikTok's creator program needs > 60 s). If a tagged campaign requires a longer minimum, the tail is extended. A
spike that lasts long extends the clip, and spikes close together are merged, up to `max_seconds` (120). The cut
happens after the post window has been recorded.

---

## 3. Configuration reference (`config/config.yaml`)

Validated at startup; unknown keys and bad values are reported as `path: problem`. Run
`docker compose run --rm clipper validate-config` after every edit.

### `global`
| key | default | meaning |
|---|---|---|
| `timezone` | Europe/Bratislava | dates of output folders, daily summary time |
| `buffer.segment_seconds` | 5 | live segment length |
| `buffer.retention_seconds` | 300 | rolling buffer length (segments of pending clips are kept longer automatically) |
| `buffer.poll_interval_seconds` | 60 | how often an offline channel is checked |
| `workers.cpu_workers` / `threads_per_worker` | 3 / 4 | render+transcribe processes and threads each (16 vCPU) |
| `workers.io_slots` | 4 | parallel downloads / cuts / uploads |
| `workers.max_attempts` | 5 | retries with exponential backoff (30 s, 60 s, 120 s …) |
| `whisper.model` | medium | model for subtitles (each clip) |
| `whisper.vod_scoring_model` | small | model for transcribing whole VODs (scoring only) |
| `whisper.compute_type` / `vad` | int8 / true | |
| `output.*` | 1080x1920, 30 fps, 8M, preset medium | encoding; `font`, `font_size`, `words_per_chunk_min/max` (3/5), `max_chars_per_line`, `subtitle_uppercase`, `highlight_active_word`, `highlight_color` |
| `delivery.backend` | local | `local` → `delivery.local_dir` (/output); `rclone` → Google Drive |
| `delivery.rclone.remote/root/config_path` | gdrive / stream-clipper / /config/rclone/rclone.conf | Drive target `remote:root/<campaign>/<date>/` |
| `delivery.delete_work_files` | true | remove temp files after delivery |
| `notifications.backend` | none | `telegram` (clips as videos) or `ntfy` (see §5) |
| `notifications.top_n` / `batch_seconds` / `daily_summary_time` | 3 / 300 / "20:00" | |
| `llm.enabled` / `ollama_url` / `model` | false / http://ollama:11434 / qwen2.5:7b-instruct | local LLM judge + hook (§3c) |

### `campaigns.<NAME>`
| key | meaning |
|---|---|
| `creator_name` | text that must appear in the caption, e.g. `@kaicenat` |
| `creator_aliases` | words identifying the creator in transcript/chat → "suggested campaigns" in the sidecar |
| `required_hashtags` | must all be in the caption (`#` is added if missing) |
| `extra_hashtags` | optional extra tags |
| `min_seconds` / `max_seconds` | allowed clip length (default 61–90) |
| `allowed_platforms` | tiktok / instagram / youtube (written into the caption file notes) |
| `caption_language` | en |
| `max_caption_chars` | 2200 |
| `hook_templates` | your own first lines, `{creator}` is replaced; default is a built-in list |
| `notes` | free text from the brief, copied under every caption |

### `sources.<NAME>`
| key | default | meaning |
|---|---|---|
| `enabled` | true | |
| `mode` | – | `live` or `vod` |
| `platform` | – | live: `twitch`, `youtube`, `kick`; vod: also `generic` (any yt-dlp URL), `gdrive` (`remote:path`), `local` (file or folder inside the container, e.g. `/data/inbox`) |
| `url` | – | channel / video / playlist / folder |
| `language` | auto | Whisper language (`en`, `sk`, …) |
| `campaigns` | [] | campaigns this source feeds |
| `layout.type` | blur_fit | `stack`, `center_crop`, `blur_fit` |
| `layout.facecam` / `gameplay` / `main` | – | crop rectangles `{x, y, w, h}` in source pixels (use the crop helper, §6) |
| `layout.facecam_height` | 700 | stack: facecam height; gameplay gets the rest (1220) |
| `layout.subtitle_y` | auto | subtitle centre as a fraction of height (default never covers the facecam) |
| `clip.pre_seconds` / `post_seconds` / `max_seconds` | 30 / 35 / 120 | |
| `clip.chat_delay_seconds` | 6 | |
| `spike.*` | see §7 | |
| `chat_backend` | auto | `twitch_irc`, `youtube_innertube`, `youtube_chat_downloader`, `youtube_api` (+ `youtube_api_key`), `kick_pusher`, `none` |
| `stream_quality` | best | streamlink quality (`720p60`, `best`, …) |
| `vod.watch` | false | poll the URL for new uploads every `watch_interval_seconds`, newest `watch_max_items` |
| `vod.top_n` | 10 | clips per video |
| `vod.min_seconds` / `max_seconds` | from campaigns | |
| `vod.keywords` | list | excitement phrases in the transcript |
| `vod.weights` | see §8 | |
| `vod.keep_download` | false | keep downloaded VOD after all its clips are delivered |

Each VOD item is processed once (tracked in `processed_items`). Live videos that are still running are skipped until they finish.

---

## 3b. Smart zoom & motion effects

Every clip gets camera movement so it doesn't look like a static screen recording
(`global.output.motion`, code in `stream_clipper/media/motion.py`):

* **Smart zoom** - fast eased punch-in to 115 % (`zoom_strength`), held ~1.6 s, smooth zoom-out. Placed on
  1. the chat-spike moment (live clips: spike time minus `chat_delay_seconds`) - always first,
  2. the loudest reactions in the clip's own audio (loudness `audio_z` robust std above the clip's normal level),
  at most `max_zooms_per_minute`, `min_gap_seconds` apart. Zoom point: the facecam for `stack`, the middle of the
  picture for `blur_fit`, upper middle for `center_crop`.
* **Motion** - slow push-in over the whole clip (`ken_burns`, 0.05 = ends at 105 %) and a short decaying camera
  shake (`shake_px`, `shake_seconds`) on the strongest moment.

Subtitles are burned in after the effect, so they never move. The planned zooms are stored in the sidecar
(`zooms`). Rendering takes ~50 % longer with effects on; `motion.enabled: false` turns everything off,
`ken_burns: 0` keeps only the punch-ins.

---

## 3c. LLM judge for live clips (Ollama, free, offline)

The `ollama` service in `docker-compose.yml` runs `qwen2.5:7b-instruct` on the CPU. The model (~5 GB) is pulled
automatically into `models/ollama/` on the first start. For every live clip, after Whisper and **before** rendering:

1. **Score 0–10.** The LLM reads the timestamped transcript, the chat-spike time and the most repeated chat messages.
   It answers a checklist: does something specific happen, is there a payoff, is it understandable without context,
   and how intense is it, plus an overall score. The 0–10 score is computed from these answers in Python, because
   7B models calibrate a plain 0–10 score badly (almost everything gets 6–7). Clips below `min_score` (5) get status
   `skipped`: no render, no Telegram message. Very strong chat spikes (strength ≥ `keep_strength`, 28) are never
   skipped, because those are often visual moments the model can't see. The reason is stored in `meta.llm`.
2. **Cut.** The cut job takes `context_seconds` (20 s) of extra footage on both sides. The LLM picks the start (the
   setup, not mid-sentence) and the end (right after the payoff). The cut is forced inside the campaign's min/max
   length and must contain the chat moment. Otherwise the planned window is used.
3. **Hook.** Generic hooks ("CHAT REACTS", "CREATOR'S REACTION") are dropped. A good ≤ 6-word title is burned in a yellow box on top for the first `hook_seconds` (3 s). It is also used
   in the captions.

Cost is ~40–60 s per clip (Ollama has 8 cores). Skipped clips save the whole render. If Ollama is down or answers garbage, the clip
is rendered exactly as before; the LLM never blocks a clip. The model only sees text (speech + chat), not the
picture. That is why a moment with little speech but a crazy chat gets a neutral 5–6. To see the decisions:

```bash
docker compose logs clipper | grep -E "llm verdict|skipped by llm"
```

Switch off: `llm.enabled: false` (everything), `live_judge: false` (keep VOD use), `trim: false`, `hook_overlay: false`.
Stricter / looser: raise / lower `min_score`.

## 4. Google Drive via rclone (headless)

Switch on when ready: `delivery.backend: rclone`.

1. On a computer **with a browser**, install rclone and run `rclone authorize "drive"`. Log in and copy the token JSON it prints.
2. On the server:
   ```bash
   docker run --rm -it -v $PWD/rclone:/config/rclone rclone/rclone config --config /config/rclone/rclone.conf
   ```
   `n` (new remote) → name `gdrive` → storage `drive` → leave client id/secret empty (or use your own, which is faster)
   → scope `1` (full) → … → "Use web browser to automatically authenticate?" **n** → paste the token from step 1.
3. Test: `docker run --rm -v $PWD/rclone:/config/rclone rclone/rclone --config /config/rclone/rclone.conf lsd gdrive:`
4. In `config.yaml`: `delivery: {backend: rclone, rclone: {remote: gdrive, root: stream-clipper}}`, then `docker compose restart`.

Files go to `gdrive:stream-clipper/<campaign>/<date>/`. A clip tagged for several campaigns is uploaded once and
copied server-side. The link in notifications is a **private** Drive link (only your account can open it); the
app never creates public share links. Local work files are deleted only after every upload succeeded; failed
uploads retry with backoff and show up in `queue ls --status failed`.

---

## 5. Notifications

**Telegram (recommended):** every clip arrives as a **video** you can save and post straight from the phone,
followed by one message per campaign with the caption in a block - tap it to copy.

1. In Telegram open **@BotFather** → `/newbot` → pick a name → copy the token (`123456:ABC...`).
2. Put it in `config/config.yaml`:
   ```yaml
   notifications:
     backend: telegram
     telegram: {bot_token: "123456:ABC...", chat_id: "", send_video: true}
     spike_alerts: true      # silent "🔥 chat spike" ping when a clip starts being made
   ```
3. Open your bot in Telegram (link from BotFather) and press **Start**.
4. `docker compose run --rm clipper telegram-test` → remembers your chat and sends a test message.
5. `docker compose restart`.

Bots may upload at most 50 MB, so a clip bigger than `max_upload_mb` (49) is sent as a smaller copy (~4-6 Mbit/s,
fine for TikTok); the full 8 Mbit/s original stays in `output/`. Leave `chat_id` empty and the first chat that
presses Start is used (stored in the DB) - so press Start before anyone else finds the bot.

**ntfy:** install the ntfy app, subscribe to a long random topic name.
```yaml
notifications: {backend: ntfy, ntfy: {server: https://ntfy.sh, topic: "clipper-8f3k2-random"}}
```
Every `batch_seconds` you get the top `top_n` new clips (score, campaign, link, caption, ⚠️ if the caption needs
fixing). At `daily_summary_time` you get counts per source/campaign, failed jobs and the top 3 of the day.

---

## 6. Crop helper (for `stack` / `center_crop`)

```bash
docker compose run --rm -v $PWD/crop:/crop clipper crop-helper https://www.twitch.tv/somechannel \
    --facecam 1450,780,470,300 --gameplay 0,0,1920,1080 --out /crop
```
Writes `crop/frame.png` (raw frame), `crop/frame_annotated.png` (grid every 100 px + your rectangles) and
`crop/preview_vertical.png` (what the clip will look like), and prints ffmpeg crop strings plus a `layout:` snippet to
paste into the config. Adjust the numbers and run it again until the preview looks right. Works with live URLs,
VOD URLs and local files (`--at 120` = frame at 2:00).

---

## 7. Tuning spike detection

How it works: every second it counts messages and unique chatters in the last `window_seconds` (10). Bots
(`ignore_users`), `ignore_patterns` (regex, default ignores `!commands`) and repeated identical lines from the same
user are filtered out first. The **average and standard deviation of the last `baseline_seconds` (300)** are
recomputed every second (spike periods excluded), so the threshold follows the chat automatically - when Kai's chat
grows from 50k to 150k viewers the threshold grows with it:

```
threshold = max(min_messages, multiplier × average, average + z_threshold × std)
spike     = messages in the last 10 s ≥ threshold        (same formula for unique chatters)
```

Example with defaults (`multiplier 2`, `z_threshold 3`): average 150 msgs/10 s, std 25 → threshold
max(60, 300, 225) = **300**. Average 4, std 2 → max(15, 8, 10) = 15 (the floor protects quiet chats).

Spikes are ignored for `warmup_seconds` (180) after a stream (re)start. `cooldown_seconds` (90) after a spike, new
spikes only extend the current clip. `metric`: `messages`, `unique`, `either` (default) or `both`. Every minute the
log shows the current numbers: `docker compose logs clipper | grep "chat stats"` →
`messages, unique, avg, std, threshold`.

`min_messages` / `min_unique_chatters` are only a floor for quiet chats; the average does the real work.

**Quality gate - only interesting moments become clips.** Fast chat alone isn't enough: polls ("yes", "1"),
numbers ("67", "5-13"), greetings, `!commands` and "vape break" chatter are ignored by default
(`ignore_patterns`), and every spike gets a strength score:

```
strength = (min(z, 30) + min(ratio, 15)) × (0.5 + reaction_share)
```

`reaction_share` = share of messages at the peak that are real reactions (LOL, LMAO, KEKW, ???, OOOO, W, "no way",
"clip it", 💀, 😂 ...). A spike needs `strength ≥ min_strength` (14) to start a clip, and each source makes at most
`max_clips_per_hour` (4) clips - over that budget only huge moments (`≥ 2 × min_strength`) get through. On a
2.7 h Jynxzi chat log this went from 26 clips to 5. Every spike is logged with its strength and top messages:
`docker compose logs clipper | grep '"spike"'` (kind `new` = clip, `weak`, `over_budget`, `extended`).
Fewer/better clips → raise `min_strength`; more → lower it.

Measure instead of guessing:
```bash
docker compose run --rm clipper record-chat --source kaicenat --minutes 60 --out /data/chat-kai.jsonl
docker compose run --rm clipper replay-chat --source kaicenat --chat /data/chat-kai.jsonl
```
`replay-chat` prints every spike the current settings would fire and the median/p95/max messages per window. Adjust
`multiplier` / `z_threshold` until you get only the real moments (a few per hour).
`docker compose up` with `command: ["run", "--record-chat"]` keeps chat logs for all live sources in `data/chatlogs/`.

Too many clips → raise `multiplier`, `z_threshold`, `cooldown_seconds`. Missing obvious
moments → lower them or check that `ignore_patterns` doesn't eat normal messages (`replay-chat` shows this).

---

## 8. Tuning VOD highlight scoring

Each second of the video gets features, robust-z-normalised per video and combined with `vod.weights`:

| weight | default | signal |
|---|---|---|
| `audio_energy` | 1.0 | loudness above the rolling 60 s median |
| `shouting` | 1.0 | sustained loud speech |
| `laughter` | 0.4 | rhythmic 3–8 Hz loudness bursts (heuristic, noisy) |
| `speech_density` | 0.6 | words per second |
| `excitement` | 0.8 | `!`, `?`, ALL CAPS, `vod.keywords` |
| `scene_density` | 0.4 | ffmpeg scene changes per 10 s |

Windows of every allowed length are scored by their mean. The best non-overlapping ones are taken greedily (top
`vod.top_n`), then start and end are snapped to sentence boundaries. Each analysed VOD leaves
`data/vod/<item>/scores.csv` (score per second) for inspection; the sidecar has the per-feature values of each clip.
Set a weight to 0 to switch a feature off (`scene_density: 0` also skips the scene-detection pass).

**Local LLM (§3c):** with `llm.enabled: true` it also rates the `top_k_candidates` (15) best heuristic windows 0–10 for "viral short", writes
hook titles for captions, and reorders the picks. It's slow on CPU (~10–30 s per window) and is never required; if it
fails, the heuristic result is used.

---

## 9. Dry run (no live stream, no upload)

```bash
mkdir -p data/inbox && cp my_video.mp4 data/inbox/
# live-style with simulated chat bursts at 2:00 and 6:40
docker compose run --rm clipper dry-run --source kaicenat --video /data/inbox/my_video.mp4 --simulate 120,400 --out /data/dryrun
# live-style with a real chat log (from record-chat; --chat-offset = epoch when the video starts)
docker compose run --rm clipper dry-run --source kaicenat --video /data/inbox/rec.mp4 --chat /data/chat.jsonl --chat-offset 1763380000 --out /data/dryrun
# VOD-style highlight scoring
docker compose run --rm clipper dry-run --source kaicenat --video /data/inbox/my_video.mp4 --vod --out /data/dryrun
```
Results go to `data/dryrun/output/`. Chat logs can also be `{"t": <seconds into video>, "user": "...", "text": "..."}` per line.

---

## 10. Operations

```bash
docker compose ps                                    # health: healthy = heartbeat < 90 s
docker compose logs -f --tail 100                    # JSON logs (source, clip_id, job_id fields)
docker compose exec clipper stream-clipper queue stats
docker compose exec clipper stream-clipper queue ls --status failed
docker compose exec clipper stream-clipper queue retry            # all failed jobs (or --id N)
docker compose build --no-cache && docker compose up -d           # update yt-dlp / streamlink (do this when a site breaks)
```

**Measured speed on this server (16 vCPU, 4 threads per worker):** Whisper medium ≈ 3× realtime (65 s clip ≈ 20 s),
Whisper small ≈ 8× realtime (3 h VOD ≈ 25 min), render of a 65 s clip ≈ 110 s (x264 preset medium). That's about
**25–30 live clips per hour with 3 workers**. That's plenty for 5 channels, since a good channel gives a few
real spikes per hour. If clips pile up (`queue stats` shows many `render queued`), set `output.preset: fast`
(about 2× faster, slightly bigger files) or reduce spikes. Live recording itself costs ~1–3 % CPU per channel
(stream copy). RAM: ~2.5 GB per CPU worker.

Disk: live buffers ≈ 300 MB per channel; VOD downloads are deleted once their clips are delivered (unless
`keep_download`); local output grows until you clean `output/` (about 65 MB per clip).

---

## 11. Troubleshooting

| symptom | fix |
|---|---|
| `validate-config` errors | read the `path: problem` lines; unknown keys are typos |
| channel never goes live in logs | check URL; `docker compose run --rm --entrypoint streamlink clipper <url>`; Twitch: channel name, YouTube: channel URL (`/live` is added) |
| `chat error` repeating | chat retries with backoff and recording continues. YouTube innertube breaks when YouTube changes things → set `chat_backend: youtube_chat_downloader` (needs `pip install chat-downloader` in the image) or `youtube_api` with a free API key |
| Kick doesn't work | Kick is **experimental**: Cloudflare sometimes blocks it. Rebuild to update yt-dlp/curl_cffi. The streamlink kick plugin needs a full browser and is not used |
| YouTube downloads fail / formats missing | rebuild the image (new yt-dlp); deno is included for YouTube's JS challenge |
| no spikes | `min_messages` too high for that chat → §7 |
| too many clips | §7 |
| subtitles over the facecam | set `layout.subtitle_y` (e.g. 0.55) |
| `crop ... exceeds source` | the stream resolution changed or the crop is wrong → crop helper |
| caption starts with `!!! DO NOT POST AS IS` | the campaign config is missing something (FILL_ME, hashtag, mention) → fix config; existing caption .txt can be edited by hand |
| container `unhealthy` | `docker compose logs`; the supervisor writes a heartbeat every 10 s |
| clips stuck | `queue ls --status failed` shows the error; `queue retry` |

---

## 12. Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # unit + integration tests (ffmpeg needed for some)
```
Layout: `stream_clipper/{config,db,queue,workers,supervisor,pipeline,captions,delivery,notify,dryrun,crophelper}.py`,
`live/` (recorder, buffer, chat backends, spikes, window), `vod/` (ingest, audio, scenes, text, scoring, llm),
`media/` (whisper, ass, layouts, render). See `PLAN.md` for the design.

Licenses: Montserrat font (SIL OFL, `assets/fonts/OFL.txt`); all dependencies are free/open source.

# Stream Clipper

Turn live streams and recorded videos into vertical clips, with a web panel for
setup and everyday management.

- **Live streams:** capture Twitch, YouTube, and experimental Kick sources; use chat
  spikes to find moments worth clipping.
- **Recorded videos (VODs):** find highlights in supported video URLs, Google Drive,
  or local files using speech, audio, and scene scoring.
- **Finished clips:** vertical video with subtitles, campaign captions, and metadata.
  Save locally or deliver to Google Drive; optionally notify through Telegram or ntfy.

The app prepares clips. You review them and post them yourself. Use content you
have permission to clip, and enter each campaign’s requirements before posting.

## Start here

- [New installation](#new-installation)
- [Migrate an existing installation](#migrate-an-existing-installation)
- [Use the web panel](#use-the-web-panel)
- [Open the panel on a remote server](#open-the-panel-on-a-remote-server)
- [Find your clips and persistent data](#find-your-clips-and-persistent-data)
- [Maintenance and troubleshooting](#maintenance-and-troubleshooting)
- [CLI and development](#cli-and-development)

For YAML keys, Google Drive authorization, notifications, crop layouts, AI scoring,
and highlight tuning, see the [advanced guide](docs/advanced.md).

To configure panel login or allowed hostnames, see [Configure .env](#configure-env)
and the supplied [.env.example](.env.example).

## New installation

You need Git, Docker, and Docker Compose. Docker is recommended on Windows; the
clipping service runs on Linux. Run the commands below from your checkout of this
repository. Shell examples use Bash.

1. Create the directories used to store configuration and application data:

   ```bash
   mkdir -p config data buffer models output rclone
   ```

   The container runs as UID/GID `1000:1000`; these directories must be writable
   by that account. Compose currently sets resource limits for a large CPU server
   (15 CPUs / 40 GB for clipper, 8 CPUs / 12 GB for Ollama). Adjust the limits in
   `docker-compose.yml` for your machine.

2. Build and start the application:

   ```bash
   docker compose up -d --build
   ```

3. Open **http://localhost:8080**. For a server on another machine, use the
   [remote access instructions](#open-the-panel-on-a-remote-server).

4. In **Campaigns**, add the creator mention, required hashtags, allowed platforms,
   and clip length rules. In **Sources**, add a stream or video and assign its
   campaign.

5. Click **Save changes**, then **Start service** on Overview.

You do not need to create or edit YAML for first-time setup. The panel writes
`config/config.yaml` for you. Models download when needed, so the first clips can
take longer.

## Migrate an existing installation

Your existing `config/config.yaml` works with the GUI. There is no separate
configuration import or database conversion. Keep your existing data directories
to retain job history, processed-video records, credentials, models, and clips.

### 1. Stop the old service and make a backup

From the **old project directory**:

```bash
docker compose down --timeout 90
mkdir -p data rclone
tar -czf "../stream-clipper-backup-$(date +%Y%m%d-%H%M%S).tar.gz" config data rclone
```

Wait for shutdown before backing up SQLite state. The archive includes configuration,
database files, and rclone credentials; keep it private. Existing `output/` and
`models/` stay in place. Back those up separately if you also want a copy of them.

If the old service runs through systemd or a direct `stream-clipper run` command,
stop it there too and disable its automatic startup. Run only one clipping service
against the same data directory.

### 2. Install the GUI version

**Updating the same repository:** if your current branch’s upstream contains the
GUI changes, update in place:

```bash
git status
git pull --ff-only
```

Commit or stash local source changes first. Check any local Compose overrides:
the clipper command must now start `web`, expose port 8080, and use the web
healthcheck from the updated `docker-compose.yml`. An old override with
`command: ["run", ...]` will bypass the panel.

**Moving to a different repository or checkout:** replace
`GUI_REPOSITORY_URL` below with the repository containing the GUI version.
Run from the stopped old project directory:

```bash
OLD_DIR="$PWD"
git clone GUI_REPOSITORY_URL "${OLD_DIR}-gui"
cd "${OLD_DIR}-gui"

for folder in config data rclone models output buffer; do
  if [ -d "$OLD_DIR/$folder" ]; then
    mkdir -p "$folder"
    cp -a "$OLD_DIR/$folder/." "$folder/"
  fi
done
```

This copies the existing installation into the new checkout. Allow enough disk
space for models, work files, and output. Copy any custom mounts, environment
settings, or reverse proxy configuration separately; retain the new GUI command,
port mapping, and healthcheck. Preserve custom host paths used by your configuration.

### 3. Validate, rebuild, and open the panel

From the updated or new project directory:

```bash
docker compose build clipper
docker compose run --rm --no-deps clipper validate-config
docker compose up -d
docker compose ps
```

Resolve validation errors before starting. Open **http://localhost:8080** and check:

- **Sources** and **Campaigns** contain your existing entries.
- **Overview** shows the service running after startup.
- **Job queue** and **Clips** show your retained history.

Compose uses `--autostart`, so clipping starts automatically when an existing
configuration is present. Saving in the panel preserves supported settings but
rewrites YAML formatting and comments.

If migration fails, stop the GUI deployment before starting the old version.
For a separate checkout, the original directories remain available. For an
in-place update, restore the previous source version and your backed-up
`config/`, `data/`, and `rclone/` while both services are stopped.

## Use the web panel

| Page | What you can do |
| --- | --- |
| **Overview** | Check service status, recent clips, queue totals, and recent service logs. Start or stop clipping and apply saved settings. |
| **Sources** | Add, edit, enable, pause, or delete stream and video sources; assign campaigns. |
| **Campaigns** | Manage creator mentions, hashtags, clip lengths, caption rules, and campaign notes. |
| **Clips** | Inspect recent clip history and processing status. Retrieve videos from your delivery folder or Google Drive. |
| **Job queue** | Inspect recent jobs and errors, refresh history, and retry failed jobs. |
| **Settings** | Configure video, subtitles, motion, transcription, AI judging, delivery, notifications, workers, and storage. |

### Save versus apply

1. Make your edits. In a source or campaign dialog, click **Keep changes**.
2. Click **Save changes** to validate and write the configuration.
3. If clipping is already running, open **Overview → Apply & restart**.

Saving alone leaves the running service on its current settings. Applying changes
flushes open clips and gives running jobs time to finish; allow about a minute.
If clipping is stopped, click **Start service** instead.

**Enabled** means a source is configured for capture, not that its channel is
currently live. **Stop service** stops clipping while keeping the panel available.
Stopping the entire Compose stack also closes the panel.

The panel detects conflicting saves from another session. Reload to get the latest
settings before editing again.

## Open the panel on a remote server

The default port is bound to localhost. From your own computer, open an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@your-server
```

Keep that connection open and visit **http://localhost:8080** on your computer.

### Configure .env

Set `CLIPPER_WEB_HOSTS` and `CLIPPER_WEB_PASSWORD` in a file named **`.env` in the
project root, beside `docker-compose.yml`**. These are deployment settings, not
fields in `config/config.yaml` or the panel’s Settings page.

1. Copy the provided template. If `.env` already exists, edit it instead:

   ```bash
   cp .env.example .env
   ```

2. For direct access through a domain or server IP, edit `.env`:

   ```dotenv
   CLIPPER_WEB_PASSWORD='replace-with-your-own-strong-password'
   CLIPPER_WEB_HOSTS=clipper.example.com,*.example.com,192.168.1.0/24,fd00::/8
   ```

   Replace these examples with your password and the hostname/IP you enter in
   your browser. Separate multiple hosts with commas. Do not include `http://`,
   `https://`, ports, or URL paths. A nonempty host list requires a password
   when host verification is enabled.
   Localhost is always allowed; leave the hosts field empty for SSH tunnel access.
   You can set a password alone to require login on localhost.

3. For direct network access, change the clipper port mapping in
   `docker-compose.yml` from `"127.0.0.1:8080:8080"` to `"8080:8080"`, or bind it
   to your intended interface. Keep the localhost binding if using an SSH tunnel
   or a reverse proxy running on the same host. Use HTTPS through a reverse proxy
   for direct remote access: Basic authentication does not encrypt HTTP traffic.

4. Apply environment and port changes by recreating the container:

   ```bash
   docker compose up -d
   ```

   Open the configured address and sign in as **admin** using your password.
   A plain `docker compose restart` does not apply changed environment variables.

Docker Compose reads `.env` automatically. Exported shell variables with the same
names take precedence. When running `stream-clipper ... web` directly without
Compose, export these variables yourself; the Python application does not load
`.env` files.

The `.env` file is ignored by Git. Keep it private, and copy it separately when
migrating to a new checkout. The [.env.example](.env.example) template contains no
credentials and documents the available variables.

### Disable host verification completely

If host verification prevents access, add this to `.env` beside `docker-compose.yml`:

```dotenv
CLIPPER_WEB_SKIP_HOST_CHECK=true
```

Then rebuild and recreate the container to load the updated code and environment:

```bash
docker compose up -d --build
```

This completely skips the application’s Host-header checks. `CLIPPER_WEB_HOSTS`
is ignored, including invalid entries. Configured password login and CSRF
protection remain active. The port binding still controls network access; this
option does not expose the localhost-only port. Set the option back to `false`
and recreate the container to restore host verification (the default).

### Host rule syntax

Supported host rules can be mixed in the same comma-separated list:

| Rule | Matches |
| --- | --- |
| `clipper.example.com` | That exact hostname (case-insensitive) |
| `*.example.com` | Subdomains such as `clipper.example.com` and `studio.dev.example.com`; add `example.com` separately to allow the root domain |
| `192.168.1.100` | That exact IPv4 address |
| `192.168.1.0/24` | Literal IPv4 hosts within that network |
| `fd00::/8` | Literal IPv6 hosts within that network |

Only a leading `*.` wildcard is supported; bare `*` and other glob patterns are
rejected at startup. IPv6 entries have no brackets in `.env`; browser URLs use
brackets, for example `http://[fd00::1]:8080`. CIDR host bits are normalized, so
`192.168.1.10/24` means `192.168.1.0/24`.

These rules check the **HTTP Host header**, not the connecting client’s address.
DNS names are not resolved for CIDR matching. Use firewall or reverse proxy rules
to restrict which client networks can connect.

## Find your clips and persistent data

With local delivery, finished files appear in `output/<campaign>/<YYYY-MM-DD>/`:

| File | Contents |
| --- | --- |
| `<clip>.mp4` | Vertical video with burned-in subtitles |
| `<clip>.<campaign>.txt` | Caption and campaign notes |
| `<clip>.json` | Source, timing, score, and other clip metadata |

Review captions marked `!!! DO NOT POST AS IS` and fix the campaign requirements
before posting. The panel shows history; it does not play or download video files.

| Host directory | Purpose |
| --- | --- |
| `config/` | Configuration, including private `config.yaml` |
| `data/` | SQLite state, job history, downloads, work files, and chat logs |
| `models/` | Cached Whisper and Ollama models |
| `buffer/` | Temporary live recording segments |
| `output/` | Finished local clips |
| `rclone/` | Google Drive authorization |

Keep these directories when upgrading. Do not delete `data/state.db` to clear
an error: it also tracks processed videos and queue state.

## Maintenance and troubleshooting

```bash
docker compose ps                     # container status
docker compose logs -f --tail 100 clipper
docker compose down --timeout 90       # stop the entire application
docker compose up -d --build           # rebuild and start after source updates
```

The Compose healthcheck confirms the **web panel** responds. Check **Overview**
for clipping-service status. Recent clipping logs are shown inside the panel;
Docker logs cover the web process and container startup.

| Problem | What to check |
| --- | --- |
| Panel will not open | Confirm the clipper container is running and port 8080 is available. For remote servers, use the SSH tunnel above. |
| “Host not allowed” | Configure `CLIPPER_WEB_HOSTS`, or set `CLIPPER_WEB_SKIP_HOST_CHECK=true` in `.env` to [disable host verification](#disable-host-verification-completely). Recreate the container after changes. |
| Container exits during migration | Inspect Docker logs and run `validate-config`; existing configurations are validated before autostart. |
| Settings cannot be saved / permission denied | Ensure bind-mounted directories are writable by UID/GID `1000:1000`. On a dedicated Linux installation, an administrator can set ownership with `sudo chown -R 1000:1000 config data buffer models output rclone`. |
| Panel says another service is running | Stop the old Docker, systemd, or CLI service. A recent heartbeat can remain for up to 90 seconds after an unexpected exit. |
| Saved settings have no effect | Choose **Apply & restart** from Overview. |
| Source produces no clips | Confirm it is enabled, live or accessible, and assigned a campaign. Inspect service logs and the [spike tuning guide](docs/advanced.md#live-spike-detection). |
| Jobs fail or get stuck | Open **Job queue**, inspect the error, fix its cause, and retry. |
| Downloads or chat stop working | Rebuild with `docker compose build --no-cache clipper`, then `docker compose up -d` to refresh provider dependencies. Kick remains experimental. |

## CLI and development

Existing CLI commands remain available. For example:

```bash
docker compose run --rm --no-deps clipper validate-config
docker compose exec clipper stream-clipper queue stats
docker compose exec clipper stream-clipper queue ls --status failed
```

For local development on Linux with Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
stream-clipper -c config/config.yaml web
```

The development panel opens on **http://localhost:8080** and does not start clipping
unless you request it. Before starting locally, update storage paths and
`fonts_dir` in Settings for your machine. Some integration tests need FFmpeg and
FFprobe; they skip automatically when those binaries are absent.

Source is in `stream_clipper/`, tests in `tests/`, and browser assets in
`stream_clipper/web_static/`. See [AGENTS.md](AGENTS.md) for contributor guidelines
and [PLAN.md](PLAN.md) for design context.

The bundled Montserrat font uses the [SIL Open Font License](assets/fonts/OFL.txt).

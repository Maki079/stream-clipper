# stream-clipper: headless auto-clipper (CPU only)
FROM denoland/deno:bin AS deno
FROM rclone/rclone:latest AS rclone

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    CLIPPER_CONFIG=/config/config.yaml \
    TZ=Europe/Bratislava

# ffmpeg (libass, x264), subtitle fonts (Noto for non-Latin + emoji), tini as PID 1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-color-emoji tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# deno: JS runtime yt-dlp needs for YouTube; rclone: Google Drive delivery
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app

# dependencies first (cached layer), then the code
COPY pyproject.toml ./
RUN mkdir -p stream_clipper && touch stream_clipper/__init__.py \
 && pip install . && pip uninstall -y stream-clipper
COPY stream_clipper ./stream_clipper
RUN pip install --no-deps .
COPY assets ./assets
COPY config/config.example.yaml /app/config.example.yaml

RUN useradd -m -u 1000 clipper \
 && mkdir -p /config /data /buffer /models /output /home/clipper/.config /home/clipper/.cache /home/clipper/.local/share \
 && chown -R clipper:clipper /config /data /buffer /models /output /home/clipper
ENV HOME=/home/clipper \
    USER=clipper \
    LOGNAME=clipper \
    XDG_CONFIG_HOME=/home/clipper/.config \
    XDG_CACHE_HOME=/home/clipper/.cache \
    XDG_DATA_HOME=/home/clipper/.local/share
# Startup repairs bind mounts as root, then drops to the clipper UID/GID before
# starting the web panel, capture supervisor, or any media processing.
USER root

HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=3 CMD ["stream-clipper", "health"]
ENTRYPOINT ["tini", "--", "python", "-m", "stream_clipper.container"]
CMD ["run"]

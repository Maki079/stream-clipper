"""Turn a configured live URL into something streamlink can record."""
from __future__ import annotations

import asyncio
import re

from ..config import SourceCfg


def twitch_channel(url: str) -> str:
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
    if not m:
        raise ValueError(f"not a twitch channel URL: {url}")
    return m.group(1).lower()


def kick_slug(url: str) -> str:
    m = re.search(r"kick\.com/([A-Za-z0-9_\-]+)", url)
    if not m:
        raise ValueError(f"not a kick channel URL: {url}")
    return m.group(1).lower()


def youtube_live_url(url: str) -> str:
    """Channel URL -> its /live URL (YouTube redirects that to the current live stream)."""
    if "watch?v=" in url or "youtu.be/" in url or url.rstrip("/").endswith("/live"):
        return url
    return url.rstrip("/") + "/live"


async def stream_input(src: SourceCfg) -> str:
    """URL to hand to streamlink. Kick goes through yt-dlp (curl_cffi impersonation) because
    streamlink's kick plugin needs a full browser."""
    if src.platform == "youtube":
        return youtube_live_url(src.url)
    if src.platform == "kick":
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--impersonate", "chrome", "-g", "-f", "best", src.url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 90)
        if proc.returncode != 0 or not out.strip():
            raise RuntimeError(f"kick resolve failed: {err.decode()[-300:]}")
        return "hls://" + out.decode().strip().splitlines()[0]
    return src.url

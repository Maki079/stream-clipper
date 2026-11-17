"""Chat backends behind one interface."""
from __future__ import annotations

from ...config import SourceCfg
from ..resolve import twitch_channel
from .base import ChatSource


def make_chat(src: SourceCfg) -> ChatSource | None:
    backend = src.resolved_chat_backend()
    if backend == "twitch_irc":
        from .twitch import TwitchChat
        return TwitchChat(twitch_channel(src.url))
    if backend == "youtube_innertube":
        from .youtube import YouTubeInnertubeChat
        return YouTubeInnertubeChat(src.url)
    if backend == "youtube_chat_downloader":
        from .youtube import YouTubeChatDownloader
        return YouTubeChatDownloader(src.url)
    if backend == "youtube_api":
        from .youtube import YouTubeApiChat
        return YouTubeApiChat(src.url, src.youtube_api_key)
    if backend == "kick_pusher":
        from .kick import KickChat
        return KickChat(src.url)
    return None

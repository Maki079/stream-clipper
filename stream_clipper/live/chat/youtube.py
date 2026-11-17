"""YouTube live chat.

Default backend: a small poller for YouTube's internal "innertube" live-chat endpoint (the
same thing pytchat/chat-downloader do; no API key, no quota, unofficial). Alternatives:
chat-downloader (optional package) and the official Data API v3 (needs a free API key).
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import AsyncIterator

import httpx

from ..resolve import youtube_live_url
from ..spikes import ChatMessage
from .base import ChatSource, now

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# EU servers get a cookie-consent interstitial without this
COOKIES = {"SOCS": "CAI", "CONSENT": "YES+cb"}


class NotLive(Exception):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, cookies=COOKIES,
                             follow_redirects=True, timeout=30)


async def resolve_video_id(client: httpx.AsyncClient, url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
    if m:
        return m.group(1)
    r = await client.get(youtube_live_url(url))
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
    if not m or '"isLive":true' not in r.text.replace(" ", ""):
        raise NotLive(f"no live stream at {url}")
    return m.group(1)


def extract_initial(html: str) -> tuple[dict, str, str]:
    """(ytInitialData, api_key, client_version) from the live_chat popout page."""
    m = re.search(r"(?:window\[\"ytInitialData\"\]|var ytInitialData|ytInitialData)\s*=\s*(\{.+?\});\s*</script>", html, re.S)
    if not m:
        raise ValueError("ytInitialData not found (chat disabled or page changed)")
    data = json.loads(m.group(1))
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    return data, key.group(1) if key else "", ver.group(1) if ver else "2.20241101.00.00"


def _continuation(conts: list[dict]) -> tuple[str | None, float]:
    for c in conts or []:
        for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            if k in c:
                return c[k].get("continuation"), c[k].get("timeoutMs", 5000) / 1000
    return None, 5.0


def parse_actions(actions: list[dict], ts: float) -> list[ChatMessage]:
    out = []
    for a in actions or []:
        item = a.get("addChatItemAction", {}).get("item", {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        text = "".join(run.get("text") or (run.get("emoji", {}).get("shortcuts") or [""])[0]
                       for run in r.get("message", {}).get("runs", []))
        user = r.get("authorName", {}).get("simpleText", "?")
        out.append(ChatMessage(ts, user, text))
    return out


class YouTubeInnertubeChat(ChatSource):
    name = "youtube_innertube"

    def __init__(self, url: str):
        self.url = url
        self._client: httpx.AsyncClient | None = None

    async def messages(self) -> AsyncIterator[ChatMessage]:
        self._client = c = _client()
        vid = await resolve_video_id(c, self.url)
        page = await c.get("https://www.youtube.com/live_chat", params={"is_popout": "1", "v": vid})
        data, key, ver = extract_initial(page.text)
        renderer = data.get("contents", {}).get("liveChatRenderer", {})
        cont, wait = _continuation(renderer.get("continuations", []))
        if not cont:
            raise NotLive("no chat continuation (stream ended or chat off)")
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}}}
        while cont:
            r = await c.post("https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
                             params={"key": key, "prettyPrint": "false"} if key else {"prettyPrint": "false"},
                             json=body | {"continuation": cont})
            r.raise_for_status()
            lcc = r.json().get("continuationContents", {}).get("liveChatContinuation", {})
            ts = now()
            for m in parse_actions(lcc.get("actions", []), ts):
                yield m
            cont, wait = _continuation(lcc.get("continuations", []))
            await asyncio.sleep(min(max(wait, 1.0), 10.0))
        raise NotLive("chat ended")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class YouTubeChatDownloader(ChatSource):
    """Uses the optional `chat-downloader` package in a thread."""

    name = "youtube_chat_downloader"

    def __init__(self, url: str):
        self.url = url
        self._stop = threading.Event()

    async def messages(self) -> AsyncIterator[ChatMessage]:
        try:
            from chat_downloader import ChatDownloader  # type: ignore
        except ImportError as e:
            raise RuntimeError("pip install chat-downloader to use this backend") from e
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(10_000)
        url = youtube_live_url(self.url)

        def worker():
            try:
                for item in ChatDownloader().get_chat(url):
                    if self._stop.is_set():
                        break
                    msg = ChatMessage(now(), item.get("author", {}).get("name", "?"), item.get("message") or "")
                    loop.call_soon_threadsafe(q.put_nowait, msg)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, e)
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        while (m := await q.get()) is not None:
            if isinstance(m, Exception):
                raise m
            yield m

    async def close(self) -> None:
        self._stop.set()


class YouTubeApiChat(ChatSource):
    """Official YouTube Data API v3 (free quota, API key). Each poll costs ~5 quota units."""

    name = "youtube_api"
    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self, url: str, api_key: str):
        self.url, self.key = url, api_key
        self._client: httpx.AsyncClient | None = None

    async def messages(self) -> AsyncIterator[ChatMessage]:
        self._client = c = _client()
        vid = await resolve_video_id(c, self.url)
        r = await c.get(f"{self.API}/videos", params={"part": "liveStreamingDetails", "id": vid, "key": self.key})
        r.raise_for_status()
        items = r.json().get("items", [])
        chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
        if not chat_id:
            raise NotLive("no activeLiveChatId")
        token = None
        first = True
        while True:
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000, "key": self.key}
            if token:
                params["pageToken"] = token
            r = await c.get(f"{self.API}/liveChat/messages", params=params)
            r.raise_for_status()
            d = r.json()
            ts = now()
            if not first:  # first page is history
                for it in d.get("items", []):
                    yield ChatMessage(ts, it["authorDetails"]["displayName"], it["snippet"].get("displayMessage", ""))
            first = False
            token = d.get("nextPageToken")
            await asyncio.sleep(max(d.get("pollingIntervalMillis", 5000) / 1000, 2))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

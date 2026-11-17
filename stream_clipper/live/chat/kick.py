"""Kick chat (EXPERIMENTAL): public Pusher websocket, read-only, no login.

The chatroom id comes from Kick's channel API, which sits behind Cloudflare; curl_cffi
browser impersonation usually gets through. The Pusher app key below is the public key
kick.com's own web client uses; override with env KICK_PUSHER_KEY if it changes.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator

import websockets

from ..resolve import kick_slug
from ..spikes import ChatMessage
from .base import ChatSource, now

PUSHER_KEY = os.environ.get("KICK_PUSHER_KEY", "32cbd69e4b950bf97679")
PUSHER_URL = f"wss://ws-us2.pusher.com/app/{PUSHER_KEY}?protocol=7&client=js&version=8.4.0&flash=false"


def chatroom_id(slug: str) -> int:
    from curl_cffi import requests as creq

    r = creq.get(f"https://kick.com/api/v2/channels/{slug}", impersonate="chrome", timeout=30)
    r.raise_for_status()
    return int(r.json()["chatroom"]["id"])


def parse_event(raw: str, ts: float) -> ChatMessage | None:
    ev = json.loads(raw)
    if ev.get("event") != "App\\Events\\ChatMessageEvent":
        return None
    d = json.loads(ev["data"]) if isinstance(ev.get("data"), str) else ev.get("data", {})
    return ChatMessage(ts, d.get("sender", {}).get("username", "?"), d.get("content", ""))


class KickChat(ChatSource):
    name = "kick_pusher"

    def __init__(self, url: str):
        self.slug = kick_slug(url)
        self._ws = None

    async def messages(self) -> AsyncIterator[ChatMessage]:
        room = await asyncio.to_thread(chatroom_id, self.slug)
        async with websockets.connect(PUSHER_URL, ping_interval=60) as ws:
            self._ws = ws
            await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
            async for raw in ws:
                if '"pusher:ping"' in raw:
                    await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    continue
                msg = parse_event(raw, now())
                if msg:
                    yield msg

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

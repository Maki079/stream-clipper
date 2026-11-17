"""Twitch chat: anonymous read-only IRC (justinfan, no OAuth)."""
from __future__ import annotations

import asyncio
import random
import ssl
from typing import AsyncIterator

from ..spikes import ChatMessage
from .base import ChatSource, now


def parse_line(line: str, ts: float) -> ChatMessage | None:
    """Parse one IRC line with IRCv3 tags; returns a ChatMessage for PRIVMSG."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    if " PRIVMSG " not in line:
        return None
    prefix, _, rest = line.partition(" PRIVMSG ")
    _, _, text = rest.partition(" :")
    user = tags.get("display-name") or prefix.lstrip(":").split("!", 1)[0]
    return ChatMessage(ts, user, text)


class TwitchChat(ChatSource):
    name = "twitch_irc"

    def __init__(self, channel: str, host: str = "irc.chat.twitch.tv", port: int = 6697):
        self.channel, self.host, self.port = channel.lower(), host, port
        self._w: asyncio.StreamWriter | None = None

    async def messages(self) -> AsyncIterator[ChatMessage]:
        r, w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port, ssl=ssl.create_default_context()), 30)
        self._w = w
        nick = f"justinfan{random.randint(10000, 99999)}"
        for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}", f"JOIN #{self.channel}"):
            w.write((line + "\r\n").encode())
        await w.drain()
        while True:
            raw = await asyncio.wait_for(r.readline(), 360)  # twitch PINGs every ~5 min
            if not raw:
                raise ConnectionError("twitch IRC closed connection")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("PING"):
                w.write(("PONG" + line[4:] + "\r\n").encode())
                await w.drain()
                continue
            if " RECONNECT" in line:
                raise ConnectionError("twitch asked to reconnect")
            msg = parse_line(line, now())
            if msg:
                yield msg

    async def close(self) -> None:
        if self._w:
            self._w.close()
            self._w = None

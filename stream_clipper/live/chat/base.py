"""ChatSource interface + resilient runner (retry with backoff, never stops recording)."""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Callable

from ... import log
from ..spikes import ChatMessage

L = log.get("chat")


class ChatSource:
    name = "base"

    def messages(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def run_chat(factory: Callable[[], ChatSource], on_message: Callable[[ChatMessage], None], source: str,
                   stop: asyncio.Event, *, max_backoff: float = 300) -> None:
    backoff = 5.0
    while not stop.is_set():
        chat = factory()
        started = time.time()
        try:
            L.info("chat connecting", extra={"source": source, "backend": chat.name})
            async for msg in chat.messages():
                on_message(msg)
                if stop.is_set():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            L.warning("chat error", extra={"source": source, "backend": chat.name, "error": str(e)[-300:]})
        finally:
            await chat.close()
        if time.time() - started > 120:
            backoff = 5.0
        try:
            await asyncio.wait_for(stop.wait(), backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, max_backoff)


def now() -> float:
    return time.time()

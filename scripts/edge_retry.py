"""Retry Edge TTS calls through rate limits and transient network errors.

Edge TTS is keyless and shared, so it answers bursts with HTTP 429 during the WebSocket
handshake. Without a retry one 429 on any of ~110 lines cost the whole day's episode.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
DEFAULT_ATTEMPTS = 7
BASE_DELAY_SECONDS = 10.0
MAX_DELAY_SECONDS = 120.0
# A short pause between lines keeps a 110-line episode from looking like a burst.
PACE_SECONDS = 0.25


def is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    try:
        import aiohttp
        from edge_tts import exceptions as edge_exceptions
    except ImportError:  # pragma: no cover - both ship with edge-tts
        return False
    if isinstance(exc, (edge_exceptions.NoAudioReceived, edge_exceptions.WebSocketError)):
        return True
    return isinstance(exc, aiohttp.ClientError)


def backoff_delay(attempt: int, base: float = BASE_DELAY_SECONDS, cap: float = MAX_DELAY_SECONDS) -> float:
    """Delay before retry number ``attempt`` (1-based): 10s, 20s, 40s, 80s, 120s ... plus a little jitter."""
    return min(cap, base * 2 ** (attempt - 1)) + random.uniform(0, 2)


async def with_retry(
    call: Callable[[], Awaitable[T]],
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    """Run ``call`` until it succeeds, retrying only errors that a later attempt can fix."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - filtered by is_retryable
            if attempt == attempts or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt)
            print(f"[tts] edge {what}: {type(exc).__name__} {getattr(exc, 'status', '')} "
                  f"(attempt {attempt}/{attempts}); retrying in {delay:.0f}s", flush=True)
            await sleep(delay)
    raise AssertionError("unreachable")

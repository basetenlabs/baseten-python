from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from baseten.sandbox._errors import SandboxGatewayError

_T = TypeVar("_T")

DEFAULT_READ_MAX_RETRIES = 5
DEFAULT_GATEWAY_MAX_RETRIES = 2
DEFAULT_UPLOAD_MAX_RETRIES = 3

_BACKOFF_BASE_SECONDS = 0.2
_BACKOFF_MAX_SECONDS = 2.0

# httpx exceptions meaning the connection dropped before a complete response
# arrived. Only then is repeating an idempotent call safe.
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class SandboxRetryOptions:
    """Retry budgets for transient failures.

    Retries only ever apply to operations that are safe to repeat, such as
    reads. Operations with side effects, such as running a process or
    creating a sandbox, are never retried. Each budget counts retries after
    the first attempt, so 0 disables that kind of retry.
    """

    read_max_retries: int = DEFAULT_READ_MAX_RETRIES
    """Retries of a read after a dropped or reset connection."""

    gateway_max_retries: int = DEFAULT_GATEWAY_MAX_RETRIES
    """Retries after the edge in front of a sandbox answers 502, 503, or 504.

    Each attempt can take as long as the edge's own timeout, so this is kept
    small.
    """

    upload_max_retries: int = DEFAULT_UPLOAD_MAX_RETRIES
    """Retries of a file upload after a dropped or reset connection."""


def is_transient_reset_error(error: object) -> bool:
    """Whether the error is a dropped or reset connection, not a response.

    A response from the server, even an error, means the request was handled;
    only a transport failure makes repeating an idempotent call safe.
    """
    return isinstance(error, _TRANSIENT_ERRORS)


def backoff_delay_seconds(attempt: int) -> float:
    """Exponential backoff capped at a maximum, plus jitter.

    The jitter is up to one base delay, so many clients failing together do
    not retry together.
    """
    capped = min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS)
    return capped + random.uniform(0, _BACKOFF_BASE_SECONDS)


def retry_idempotent(
    call: Callable[[], _T],
    *,
    max_retries: int,
    gateway_max_retries: int,
) -> _T:
    """Run an idempotent call, retrying dropped connections and edge errors.

    Must never wrap a call with side effects, since a dropped connection does
    not tell whether the server acted on it.
    """
    attempt = 0
    gateway_attempt = 0
    while True:
        try:
            return call()
        except SandboxGatewayError:
            gateway_attempt += 1
            if gateway_attempt > gateway_max_retries:
                raise
            delay = backoff_delay_seconds(gateway_attempt)
        except Exception as error:
            attempt += 1
            if attempt > max_retries or not is_transient_reset_error(error):
                raise
            delay = backoff_delay_seconds(attempt)
        time.sleep(delay)


async def retry_idempotent_async(
    call: Callable[[], Awaitable[_T]],
    *,
    max_retries: int,
    gateway_max_retries: int,
) -> _T:
    """Async variant of :func:`retry_idempotent`."""
    attempt = 0
    gateway_attempt = 0
    while True:
        try:
            return await call()
        except SandboxGatewayError:
            gateway_attempt += 1
            if gateway_attempt > gateway_max_retries:
                raise
            delay = backoff_delay_seconds(gateway_attempt)
        except Exception as error:
            attempt += 1
            if attempt > max_retries or not is_transient_reset_error(error):
                raise
            delay = backoff_delay_seconds(attempt)
        await asyncio.sleep(delay)

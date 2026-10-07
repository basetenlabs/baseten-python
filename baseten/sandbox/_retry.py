from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from baseten.sandbox._errors import SandboxAPIError, SandboxGatewayError

_T = TypeVar("_T")

_DEFAULT_READ_MAX_RETRIES = 5
_DEFAULT_GATEWAY_MAX_RETRIES = 2
_DEFAULT_UPLOAD_MAX_RETRIES = 3

# Retry backoff bounds in seconds. Module variables so tests can shorten them.
_backoff_base = 0.2
_backoff_max = 2.0

# Texts of the protocol errors httpx raises when a connection dropped before
# a response arrived, or HTTP/2 reset a stream or closed the connection. Other
# protocol errors, such as a malformed response, are not transient.
_TRANSIENT_PROTOCOL_ERROR_MARKERS = (
    # HTTP/1.1 and HTTP/2: the connection closed before a response.
    "Server disconnected",
    # HTTP/1.1: the connection closed partway through the body.
    "peer closed connection without sending complete message body",
    # HTTP/2: the server reset one stream, including when rate limiting
    # stream resets with ENHANCE_YOUR_CALM.
    "StreamReset",
    # HTTP/2: the server sent GOAWAY.
    "ConnectionTerminated",
)

# How far down an error's cause chain to look.
_MAX_CAUSE_DEPTH = 10


@dataclass(kw_only=True)
class SandboxRetryOptions:
    """Retry budgets for transient failures.

    Retries only ever apply to operations that are safe to repeat, such as
    reads. Operations with side effects, such as running a process or creating
    a sandbox, are never retried. Each budget counts retries after the first
    attempt, so 0 disables that kind of retry.
    """

    read_max_retries: int | None = None
    """Retries of a read after a dropped or reset connection. Defaults to 5."""

    gateway_max_retries: int | None = None
    """Retries after the edge in front of a sandbox answers 502, 503, or 504.

    Each attempt can take as long as the edge's own timeout, so this is kept
    small. Defaults to 2.
    """

    upload_max_retries: int | None = None
    """Retries of a file upload after a dropped or reset connection. Defaults to 3."""


@dataclass(frozen=True)
class ResolvedRetryOptions:
    """Retry budgets with the defaults filled in."""

    read_max_retries: int
    gateway_max_retries: int
    upload_max_retries: int


def resolve_retry_options(options: SandboxRetryOptions | None) -> ResolvedRetryOptions:
    """Fill in the default retry budgets."""
    options = options or SandboxRetryOptions()
    return ResolvedRetryOptions(
        read_max_retries=_DEFAULT_READ_MAX_RETRIES
        if options.read_max_retries is None
        else options.read_max_retries,
        gateway_max_retries=_DEFAULT_GATEWAY_MAX_RETRIES
        if options.gateway_max_retries is None
        else options.gateway_max_retries,
        upload_max_retries=_DEFAULT_UPLOAD_MAX_RETRIES
        if options.upload_max_retries is None
        else options.upload_max_retries,
    )


def is_transient_reset_error(err: BaseException) -> bool:
    """Report whether an error is a dropped or reset connection.

    Only then, as opposed to a response from the server, is repeating an
    idempotent call safe.
    """
    # A response came back, so the server handled the request, even when its
    # body happens to contain one of the markers.
    if isinstance(err, SandboxAPIError):
        return False
    current: BaseException | None = err
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            break
        # Reset, refused, aborted, and broken pipe, on every platform.
        if isinstance(current, ConnectionError):
            return True
        # Only the operating system's connection timeout carries an errno; a
        # socket timeout the caller configured has none, and is theirs to
        # handle.
        if isinstance(current, TimeoutError) and current.errno is not None:
            return True
        if isinstance(current, httpx.RemoteProtocolError) and any(
            marker in str(current) for marker in _TRANSIENT_PROTOCOL_ERROR_MARKERS
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def backoff_delay(attempt: int) -> float:
    """Return the seconds to wait before a retry.

    Exponential backoff capped at a maximum, plus up to one base delay of
    jitter so that many clients failing together do not retry together.
    """
    capped = min(_backoff_base * 2 ** (attempt - 1), _backoff_max)
    return capped + random.uniform(0, _backoff_base)


def retry_idempotent(
    call: Callable[[], _T], *, max_retries: int, gateway_max_retries: int
) -> _T:
    """Run an idempotent call, retrying transient failures.

    Dropped connections and edge gateway errors are retried on separate
    budgets. Must never wrap a call with side effects, since a dropped
    connection does not tell whether the server acted on it.
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
            delay = backoff_delay(gateway_attempt)
        except Exception as err:
            attempt += 1
            if attempt > max_retries or not is_transient_reset_error(err):
                raise
            delay = backoff_delay(attempt)
        time.sleep(delay)


async def retry_idempotent_async(
    call: Callable[[], Awaitable[_T]], *, max_retries: int, gateway_max_retries: int
) -> _T:
    """Async form of :func:`retry_idempotent`."""
    attempt = 0
    gateway_attempt = 0
    while True:
        try:
            return await call()
        except SandboxGatewayError:
            gateway_attempt += 1
            if gateway_attempt > gateway_max_retries:
                raise
            delay = backoff_delay(gateway_attempt)
        except Exception as err:
            attempt += 1
            if attempt > max_retries or not is_transient_reset_error(err):
                raise
            delay = backoff_delay(attempt)
        await asyncio.sleep(delay)

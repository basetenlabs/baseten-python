from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

# Refresh before the stated expiry: clock skew, plus time in flight.
_TOKEN_EXPIRY_LEEWAY = timedelta(seconds=60)

# A few retries cover a new token invalidated in the same event as the old
# one; beyond that, rejection points at something a new token cannot fix.
_TOKEN_INVALIDATION_MAX_RETRIES = 2

# A revocation rejects every token issued before its cutoff, the event's time
# rounded up to the next whole second. A token minted right after the event
# can fall before the cutoff and be rejected too, so after the first resend,
# each one waits this long to be minted past it. A module variable so tests
# can shorten it.
_TOKEN_REVOKED_RETRY_DELAY_SECONDS = 1.0

# Both planes send this header on revocation, which can precede the stated
# expiry. The request was untouched, so re-sending with a new token is safe
# even when it has side effects.
_TOKEN_REVOKED_CODE = "TOKEN_REVOKED"


class SandboxTokenProvider(Protocol):
    """Returns the bearer token to send on a request. Called for every request."""

    def __call__(self) -> str: ...


class AsyncSandboxTokenProvider(Protocol):
    """Returns the bearer token to send on a request, or an awaitable of one."""

    def __call__(self) -> str | Awaitable[str]: ...


def resolve_http2_flag(http2: bool | None) -> bool:
    """Resolve the HTTP/2 option to a concrete flag for httpx.

    ``None`` enables HTTP/2 when the optional ``h2`` package is importable;
    ``True`` requires it and fails otherwise; ``False`` never uses it.
    """
    if http2 is False:
        return False
    try:
        import h2  # ty: ignore[unresolved-import]  # noqa: F401
    except ImportError:
        if http2 is True:
            raise ValueError(
                "http2=True requires the h2 package; install baseten with the"
                " http2 extra (pip install baseten[http2])"
            )
        return False
    return True


class _TokenCache:
    """A cached minted token, guarded so concurrent callers share one mint.

    The lock spans the miss check and the mint, so a burst on a cold cache
    sends a single exchange.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def get_or_mint(
        self, mint: Callable[[], tuple[str, datetime]]
    ) -> tuple[str, datetime]:
        with self._lock:
            if (
                self._token is not None
                and self._expires_at is not None
                and datetime.now(UTC) < self._expires_at - _TOKEN_EXPIRY_LEEWAY
            ):
                return self._token, self._expires_at
            self._token, self._expires_at = mint()
            return self._token, self._expires_at

    def drop(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one.

        Compared by value, so a rejection of an older token never discards a
        newer one another request already minted.
        """
        with self._lock:
            if self._token == token:
                self._token = None
                self._expires_at = None


class SyncTokenSource:
    """Where each request's bearer token comes from: a caller's token provider,
    or a token minted from an API key, cached until shortly before it expires.

    The API key is exchanged for a token because the sandbox APIs do not
    accept API keys directly. Should that change, only this class changes.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: SandboxTokenProvider | None,
        mint: Callable[[], tuple[str, datetime]] | None,
    ) -> None:
        if token_provider is not None and api_key != "":
            raise ValueError("api_key must be empty when token_provider is set")
        self._provider = token_provider
        self._mint = mint
        self._cache = _TokenCache()

    def token(self) -> str | None:
        """The token for a request, or None to send no Authorization.

        None is the advanced opt-out of an empty API key with no provider.
        """
        if self._provider is not None:
            return self._provider()
        if self._mint is None:
            return None
        token, _ = self._cache.get_or_mint(self._mint)
        return token

    def invalidate(self, token: str) -> None:
        self._cache.drop(token)


class AsyncTokenSource:
    """Async variant of :class:`SyncTokenSource`.

    The token provider may return a string or an awaitable of one. The
    asyncio lock makes concurrent callers share one mint.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: SandboxTokenProvider | AsyncSandboxTokenProvider | None,
        mint: Callable[[], Awaitable[tuple[str, datetime]]] | None,
    ) -> None:
        if token_provider is not None and api_key != "":
            raise ValueError("api_key must be empty when token_provider is set")
        self._provider = token_provider
        self._mint = mint
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def token(self) -> str | None:
        if self._provider is not None:
            provided = self._provider()
            if isinstance(provided, str):
                return provided
            return await provided
        if self._mint is None:
            return None
        async with self._lock:
            if (
                self._token is not None
                and self._expires_at is not None
                and datetime.now(UTC) < self._expires_at - _TOKEN_EXPIRY_LEEWAY
            ):
                return self._token
            token, expires_at = await self._mint()
            self._token = token
            self._expires_at = expires_at
            return token

    async def invalidate(self, token: str) -> None:
        async with self._lock:
            if self._token == token:
                self._token = None
                self._expires_at = None


class _SyncAuthTransport(httpx.BaseTransport):
    """Adds a bearer token from the source to every request.

    A request whose token was revoked is re-sent with a fresh one, up to
    :data:`_TOKEN_INVALIDATION_MAX_RETRIES` times. Only a body already in
    memory is re-sent: one still backed by a stream is sent once, since
    replaying it would mean materializing it here.
    """

    def __init__(
        self, inner: httpx.BaseTransport, token_source: SyncTokenSource
    ) -> None:
        self._inner = inner
        self._token_source = token_source

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # A body still backed by a stream, such as a raw multipart upload,
        # cannot be replayed, and materializing it here is the memory
        # exhaustion to avoid: it is sent once, without revocation resend.
        try:
            _ = request.content
            replayable = True
        except httpx.RequestNotRead:
            replayable = False
        for attempt in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = self._token_source.token()
            if token is not None:
                request.headers["Authorization"] = f"Bearer {token}"
            response = self._inner.handle_request(request)
            revoked = (
                replayable
                and token is not None
                and response.status_code == 401
                and response.headers.get("x-blaxel-error-code") == _TOKEN_REVOKED_CODE
            )
            # The last attempt's response is returned even when still
            # revoked, so the generated client raises a meaningful error.
            if not revoked or attempt == _TOKEN_INVALIDATION_MAX_RETRIES:
                return response
            self._token_source.invalidate(token)
            response.close()
            if attempt > 0:
                time.sleep(_TOKEN_REVOKED_RETRY_DELAY_SECONDS)
        raise AssertionError  # pragma: no cover - loop returns first

    def close(self) -> None:
        self._inner.close()


class _AsyncAuthTransport(httpx.AsyncBaseTransport):
    """Async variant of :class:`_SyncAuthTransport`."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, token_source: AsyncTokenSource
    ) -> None:
        self._inner = inner
        self._token_source = token_source

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # A body still backed by an async stream, such as a raw multipart
        # upload, cannot be replayed, and materializing it here is the
        # memory exhaustion to avoid: it is sent once, without revocation
        # resend.
        try:
            _ = request.content
            replayable = True
        except httpx.RequestNotRead:
            replayable = False
        for attempt in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = await self._token_source.token()
            if token is not None:
                request.headers["Authorization"] = f"Bearer {token}"
            response = await self._inner.handle_async_request(request)
            revoked = (
                replayable
                and token is not None
                and response.status_code == 401
                and response.headers.get("x-blaxel-error-code") == _TOKEN_REVOKED_CODE
            )
            # The last attempt's response is returned even when still
            # revoked, so the generated client raises a meaningful error.
            if not revoked or attempt == _TOKEN_INVALIDATION_MAX_RETRIES:
                return response
            await self._token_source.invalidate(token)
            await response.aclose()
            if attempt > 0:
                await asyncio.sleep(_TOKEN_REVOKED_RETRY_DELAY_SECONDS)
        raise AssertionError  # pragma: no cover - loop returns first

    async def aclose(self) -> None:
        await self._inner.aclose()

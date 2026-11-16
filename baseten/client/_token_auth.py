"""What every short-lived Baseten token needs.

A cache that mints a new token shortly before the current one expires, and an
:class:`httpx.Auth` that sends a request again with a new token when the server
refuses the current one.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx


@dataclass(frozen=True)
class AccessToken:
    """A short-lived Baseten access token, sent as ``Authorization: Bearer``."""

    token: str
    """The bearer token."""

    expires_at: datetime
    """When the token expires."""


@runtime_checkable
class AccessTokenSource(Protocol):
    """Supplies a current access token.

    A client configured with one calls it before every request, so the source
    decides when to refresh.
    """

    def access_token(self) -> AccessToken:
        """Return a current access token."""
        ...


@runtime_checkable
class AsyncAccessTokenSource(Protocol):
    """Async form of :class:`AccessTokenSource`."""

    async def access_token(self) -> AccessToken:
        """Return a current access token."""
        ...


@runtime_checkable
class AccessTokenInvalidator(Protocol):
    """Implemented by an access token source that caches tokens.

    When Baseten refuses a token before it expires, for example because it was
    revoked, a client drops it with :meth:`invalidate_access_token`, asks the
    source for a new one, and sends the request again. Clients resend only for
    sources that implement this.
    """

    def invalidate_access_token(self, token: str) -> None:
        """Drop *token* if it is still the cached one."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _CachedToken:
    token: AccessToken
    refresh_at: datetime


def _cached(
    token: AccessToken, *, minted_at: datetime, refresh_before: timedelta
) -> _CachedToken:
    """Cache *token* until *refresh_before* its expiry, capped at half its lifetime.

    The cap keeps a token whose lifetime is shorter than the margin from being
    minted again on every call.
    """
    lifetime = max(token.expires_at - minted_at, timedelta(0))
    return _CachedToken(
        token=token,
        refresh_at=token.expires_at - min(refresh_before, lifetime / 2),
    )


class TokenCache:
    """Holds one token and mints a new one when it is due for refresh.

    Safe for use from several threads. Concurrent callers on a cold cache share
    one mint. *mint* must bound its own duration, since every waiting caller
    waits on it.
    """

    def __init__(
        self,
        *,
        mint: Callable[[], AccessToken],
        refresh_before: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an empty cache; the first mint happens on first use."""
        self._mint = mint
        self._refresh_before = refresh_before
        self._now = now
        self._lock = threading.Lock()
        self._current: _CachedToken | None = None

    def token(self) -> AccessToken:
        """Return the cached token, minting one when none is cached or it is due."""
        # Held across the mint, so a burst on a cold cache sends one mint. A
        # caller that waited on a failed mint then makes one of its own.
        with self._lock:
            current = self._current
            if current is not None and self._now() < current.refresh_at:
                return current.token
            minted_at = self._now()
            token = self._mint()
            self._current = _cached(
                token, minted_at=minted_at, refresh_before=self._refresh_before
            )
            # Returned even if already due, so a skewed clock cannot make every
            # call mint.
            return token

    def invalidate(self, token: str) -> None:
        """Drop *token* if it is still the cached one."""
        with self._lock:
            # Compared by value, so a refusal of an older token never discards
            # a newer one another caller already minted.
            if self._current is not None and self._current.token.token == token:
                self._current = None


class AsyncTokenCache:
    """Async form of :class:`TokenCache`.

    The shared mint runs as its own task, so one caller being cancelled does
    not cancel the mint the others wait on.
    """

    def __init__(
        self,
        *,
        mint: Callable[[], Awaitable[AccessToken]],
        refresh_before: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an empty cache; the first mint happens on first use."""
        self._mint = mint
        self._refresh_before = refresh_before
        self._now = now
        self._current: _CachedToken | None = None
        self._minting: asyncio.Task[_CachedToken] | None = None

    async def token(self) -> AccessToken:
        """Return the cached token, as :meth:`TokenCache.token` does."""
        while True:
            current = self._current
            if current is not None and self._now() < current.refresh_at:
                return current.token
            started = self._minting is None
            if self._minting is None:
                self._minting = asyncio.create_task(self._run_mint())
                # Retrieved here so a failure no caller is left to see is not
                # reported as never retrieved.
                self._minting.add_done_callback(
                    lambda task: task.cancelled() or task.exception()
                )
            try:
                minted = await asyncio.shield(self._minting)
            except Exception:
                # Another caller's failed mint gets one more try of this
                # caller's own; the failed one already left the cache.
                if started:
                    raise
                continue
            # A token this caller just minted is used even if already due, so
            # a skewed clock cannot loop here forever.
            if started or self._now() < minted.refresh_at:
                return minted.token
            if self._current is minted:
                self._current = None

    def invalidate(self, token: str) -> None:
        """Drop *token* if it is still the cached one."""
        # Compared by value, so a refusal of an older token never discards a
        # newer one another caller already minted.
        if self._current is not None and self._current.token.token == token:
            self._current = None

    async def _run_mint(self) -> _CachedToken:
        try:
            minted_at = self._now()
            token = await self._mint()
            self._current = _cached(
                token, minted_at=minted_at, refresh_before=self._refresh_before
            )
            return self._current
        finally:
            self._minting = None


# A few resends cover a new token being invalidated in the same event as the
# old one. Past that, the refusal is returned, since tokens that keep getting
# refused point at something a new token cannot fix.
_MAX_RESENDS = 2

# Revocation refuses every token issued before a cutoff, the time of the
# revoking event rounded up to the next whole second. A token minted right
# after the event can fall before the cutoff and be refused too, so after the
# first resend, each one waits this long to be minted past it. A module
# variable so tests can shorten it.
_resend_delay_sec = 1.0


class TokenAuth(httpx.Auth):
    """Authenticates requests with ``Authorization: Bearer`` from *get_token*.

    *get_token* is called with ``None``, or with the token the server just
    refused, and returns the token to send, or ``None`` to send no
    Authorization. When the server refuses the token (see
    :func:`is_token_refused`) and the body can be sent again, the request is
    resent with a new token, at most twice.
    """

    def __init__(
        self,
        get_token: Callable[[str | None], str | None],
        *,
        resend_refused: bool = True,
        origin: str | None = None,
    ) -> None:
        """Create the auth.

        Args:
            get_token: Returns the token for a request.
            resend_refused: Whether to resend a request whose token was refused.
            origin: When set, only requests to this URL's host and port that
                carry no Authorization of their own are authenticated, so a
                token never reaches another host through a shared HTTP client.
        """
        self._get_token = get_token
        self._resend_refused = resend_refused
        self._origin = None if origin is None else httpx.URL(origin)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request, again with a new token if its token was refused."""
        if not _applies_to(request, self._origin):
            yield request
            return
        resend = self._resend_refused and _is_replayable(request)
        refused: str | None = None
        for attempt in range(_MAX_RESENDS + 1):
            token = self._get_token(refused)
            _set_authorization(request, token)
            response = yield request
            if token is None or not resend or not is_token_refused(response):
                return
            if attempt == _MAX_RESENDS:
                return
            refused = token
            if attempt > 0:
                time.sleep(_resend_delay_sec)


class AsyncTokenAuth(httpx.Auth):
    """Async form of :class:`TokenAuth`."""

    def __init__(
        self,
        get_token: Callable[[str | None], Awaitable[str | None]],
        *,
        resend_refused: bool = True,
        origin: str | None = None,
    ) -> None:
        """Create the auth, as :class:`TokenAuth` does."""
        self._get_token = get_token
        self._resend_refused = resend_refused
        self._origin = None if origin is None else httpx.URL(origin)

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Send the request, again with a new token if its token was refused."""
        if not _applies_to(request, self._origin):
            yield request
            return
        resend = self._resend_refused and _is_replayable(request)
        refused: str | None = None
        for attempt in range(_MAX_RESENDS + 1):
            token = await self._get_token(refused)
            _set_authorization(request, token)
            response = yield request
            if token is None or not resend or not is_token_refused(response):
                return
            if attempt == _MAX_RESENDS:
                return
            refused = token
            if attempt > 0:
                await asyncio.sleep(_resend_delay_sec)


def is_token_refused(response: httpx.Response) -> bool:
    """Report whether the server refused a request's token before its expiry.

    For example after it was revoked. The request was refused before any work
    was done, so sending it again with a new token is safe even when it has
    side effects.
    """
    if response.status_code != 401:
        return False
    # Sandboxes, and the control plane for sandbox tokens, send this header.
    if response.headers.get("x-blaxel-error-code") == "TOKEN_REVOKED":
        return True
    # The RFC 6750 response to a refused access token.
    return any(
        'error="invalid_token"' in challenge
        for challenge in response.headers.get_list("www-authenticate")
    )


def _applies_to(request: httpx.Request, origin: httpx.URL | None) -> bool:
    if origin is None:
        return True
    return (
        request.url.host == origin.host
        and request.url.port == origin.port
        and "Authorization" not in request.headers
    )


def _is_replayable(request: httpx.Request) -> bool:
    """Report whether a request's body can be sent again."""
    # A stream body can be read only once, so a request carrying one is never
    # sent again. The SDK itself never sends one; only raw API calls can.
    return isinstance(request.stream, httpx.ByteStream)


def _set_authorization(request: httpx.Request, token: str | None) -> None:
    if token is not None:
        request.headers["Authorization"] = f"Bearer {token}"


def check_client_auth(
    *,
    api_key: str | None,
    credentials: object | None,
    headers: Mapping[str, str] | None,
) -> None:
    """Check that a client is given exactly one way to authenticate."""
    if api_key is not None and credentials is not None:
        raise ValueError("api_key and credentials are mutually exclusive")
    if api_key is None and credentials is None:
        raise ValueError("one of api_key or credentials is required")
    if credentials is not None and any(
        key.lower() == "authorization" for key in headers or {}
    ):
        raise ValueError("headers must not set Authorization when credentials is set")


def credentials_auth(credentials: AccessTokenSource, *, origin: str) -> TokenAuth:
    """Build the auth that sends *credentials*' token to *origin* only."""
    if inspect.iscoroutinefunction(credentials.access_token):
        raise TypeError(
            "credentials.access_token is async; use the client's async form"
        )
    invalidator = (
        credentials if isinstance(credentials, AccessTokenInvalidator) else None
    )

    def get_token(refused: str | None) -> str:
        if refused is not None and invalidator is not None:
            invalidator.invalidate_access_token(refused)
        return _non_empty(credentials.access_token())

    # Without a way to drop the refused token, the source would return it
    # again, so the request is sent once.
    return TokenAuth(get_token, resend_refused=invalidator is not None, origin=origin)


def async_credentials_auth(
    credentials: AsyncAccessTokenSource, *, origin: str
) -> AsyncTokenAuth:
    """Async form of :func:`credentials_auth`."""
    if not inspect.iscoroutinefunction(credentials.access_token):
        raise TypeError(
            "credentials.access_token must be async; use the client's sync form"
        )
    invalidator = (
        credentials if isinstance(credentials, AccessTokenInvalidator) else None
    )

    async def get_token(refused: str | None) -> str:
        if refused is not None and invalidator is not None:
            invalidator.invalidate_access_token(refused)
        return _non_empty(await credentials.access_token())

    return AsyncTokenAuth(
        get_token, resend_refused=invalidator is not None, origin=origin
    )


def _non_empty(token: AccessToken) -> str:
    if token.token == "":
        raise ValueError("the access token source returned an empty token")
    return token.token

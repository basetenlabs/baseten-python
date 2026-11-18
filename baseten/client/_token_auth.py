"""What every short-lived Baseten token needs.

A cache that mints a new token shortly before the current one expires, and an
:class:`httpx.Auth` that sends a token from :class:`Credentials`.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx


@dataclass(frozen=True)
class AccessToken:
    """A short-lived Baseten access token, sent as ``Authorization: Bearer``."""

    token: str = field(repr=False)
    """The bearer token, left out of the repr so logging it does not leak it."""

    expires_at: datetime
    """When the token expires."""


@dataclass(frozen=True)
class AccessTokenOptions:
    """What :class:`Credentials` are told about the token they are asked for."""

    refused_token: str | None = field(default=None, repr=False)
    """A token Baseten refused before its stated expiry.

    Credentials that cache tokens must not return it again.
    """


class Credentials(Protocol):
    """Supplies a current Baseten access token.

    A client given one as ``credentials`` calls it before every request, so
    the credentials decide when to refresh.
    """

    def access_token(self, options: AccessTokenOptions) -> AccessToken:
        """Return a current access token."""
        ...


class AsyncCredentials(Protocol):
    """Async form of :class:`Credentials`."""

    async def access_token(self, options: AccessTokenOptions) -> AccessToken:
        """Return a current access token."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _CachedToken:
    token: AccessToken
    refresh_at: datetime


def _cached(
    token: AccessToken, *, minted_at: datetime, refresh_leeway: timedelta
) -> _CachedToken:
    """Cache *token* until *refresh_leeway* before expiry, capped at half its lifetime.

    The cap keeps a token whose lifetime is shorter than the leeway from being
    minted again on every call.
    """
    lifetime = max(token.expires_at - minted_at, timedelta(0))
    return _CachedToken(
        token=token,
        refresh_at=token.expires_at - min(refresh_leeway, lifetime / 2),
    )


class _Mint:
    """One call to a :class:`TokenCache`'s mint; its result is set before *done*."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: _CachedToken | None = None
        self.error: BaseException | None = None


class TokenCache:
    """Holds one token and mints a new one when it is due for refresh.

    Safe for use from several threads. Concurrent callers share one mint,
    which runs on the thread of the caller that started it. *mint* must bound
    its own duration, since every waiting caller waits on it.
    """

    def __init__(
        self,
        *,
        mint: Callable[[], AccessToken],
        refresh_leeway: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an empty cache; the first mint happens on first use."""
        self._mint = mint
        self._refresh_leeway = refresh_leeway
        self._now = now
        self._lock = threading.Lock()
        self._current: _Mint | None = None
        # The token last dropped for being due for refresh. Still valid until
        # it expires, so it stands in when the refresh fails.
        self._previous: AccessToken | None = None

    def token(self) -> AccessToken:
        """Return the cached token, minting one when none is cached or it is due."""
        joined_failure = False
        while True:
            with self._lock:
                mint = self._current
                started = mint is None
                if mint is None:
                    mint = _Mint()
                    self._current = mint
            if started:
                self._run_mint(mint)
            else:
                mint.done.wait()
            if mint.error is not None:
                # Interrupted rather than failed, so the starter's own
                # exception is not raised on another thread.
                if not started and not isinstance(mint.error, Exception):
                    continue
                # Another caller's failed mint gets one more try, which may
                # be this caller's own or one shared with other such callers;
                # the failed one already left the cache.
                if not started and not joined_failure:
                    joined_failure = True
                    continue
                fallback = self._fallback()
                if fallback is not None:
                    return fallback
                raise mint.error
            assert mint.result is not None
            # A token this caller just minted is used even if already due, so
            # a skewed clock cannot loop here forever.
            if started or self._now() < mint.result.refresh_at:
                return mint.result.token
            with self._lock:
                if self._current is mint:
                    self._current = None
                    self._previous = mint.result.token

    def invalidate(self, token: str) -> None:
        """Drop *token* if it is still the cached one."""
        with self._lock:
            if self._previous is not None and self._previous.token == token:
                self._previous = None
            current = self._current
            # Compared by value, so a refusal of an older token never discards
            # a newer one another caller already minted. One still minting is
            # newer than any token a caller could have sent.
            if (
                current is not None
                and current.result is not None
                and current.result.token.token == token
            ):
                self._current = None

    def _fallback(self) -> AccessToken | None:
        # A refresh that fails while the replaced token is still valid falls
        # back to it, so a brief outage of the minting endpoint does not fail
        # requests the old token can still serve.
        with self._lock:
            previous = self._previous
        if previous is not None and self._now() < previous.expires_at:
            return previous
        return None

    def _run_mint(self, mint: _Mint) -> None:
        """Run *mint*, keeping a ``KeyboardInterrupt`` or the like propagating."""
        try:
            minted_at = self._now()
            mint.result = _cached(
                self._mint(), minted_at=minted_at, refresh_leeway=self._refresh_leeway
            )
            with self._lock:
                self._previous = None
        except BaseException as e:
            mint.error = e
            # Not cached, so the next caller tries again.
            with self._lock:
                if self._current is mint:
                    self._current = None
            if not isinstance(e, Exception):
                raise
        finally:
            mint.done.set()


class AsyncTokenCache:
    """Async form of :class:`TokenCache`.

    The shared mint runs as its own task, so one caller being cancelled does
    not cancel the mint the others wait on.
    """

    def __init__(
        self,
        *,
        mint: Callable[[], Awaitable[AccessToken]],
        refresh_leeway: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an empty cache; the first mint happens on first use."""
        self._mint = mint
        self._refresh_leeway = refresh_leeway
        self._now = now
        self._current: asyncio.Task[_CachedToken] | None = None
        # As in TokenCache.
        self._previous: AccessToken | None = None

    async def token(self) -> AccessToken:
        """Return the cached token, as :meth:`TokenCache.token` does."""
        joined_failure = False
        while True:
            task = self._current
            started = task is None
            if task is None:
                task = asyncio.create_task(self._run_mint())
                # Retrieved here so a failure no caller is left to see is not
                # reported as never retrieved.
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
                self._current = task
            try:
                minted = await asyncio.shield(task)
            except Exception:
                # As in TokenCache: at most one more try after joining
                # another caller's failed mint.
                if not started and not joined_failure:
                    joined_failure = True
                    continue
                previous = self._previous
                if previous is not None and self._now() < previous.expires_at:
                    return previous
                raise
            # A token this caller just minted is used even if already due, so
            # a skewed clock cannot loop here forever.
            if started or self._now() < minted.refresh_at:
                return minted.token
            if self._current is task:
                self._current = None
                self._previous = minted.token

    def invalidate(self, token: str) -> None:
        """Drop *token* if it is still the cached one."""
        if self._previous is not None and self._previous.token == token:
            self._previous = None
        current = self._current
        # Compared by value, so a refusal of an older token never discards a
        # newer one another caller already minted.
        if (
            current is not None
            and current.done()
            and not current.cancelled()
            and current.exception() is None
            and current.result().token.token == token
        ):
            self._current = None

    async def _run_mint(self) -> _CachedToken:
        try:
            minted_at = self._now()
            minted = _cached(
                await self._mint(),
                minted_at=minted_at,
                refresh_leeway=self._refresh_leeway,
            )
        except BaseException:
            # Not cached, so the next caller tries again.
            if self._current is asyncio.current_task():
                self._current = None
            raise
        self._previous = None
        return minted


class CredentialsAuth(httpx.Auth):
    """Sends a token from :class:`Credentials` on each request to one origin.

    A token is fetched for every request, so a long-lived client picks up
    refreshed tokens. Each request is sent once; a refusal is returned to the
    caller as is, and when Baseten refused the token, the next request asks
    the credentials for a new one.
    """

    def __init__(self, credentials: Credentials, *, origin: str) -> None:
        """Create the auth.

        Args:
            credentials: Supplies the token.
            origin: Only requests to this URL's scheme, host and port that
                carry no Authorization of their own are authenticated, so a
                token never reaches another host through a shared HTTP client.
        """
        if inspect.iscoroutinefunction(credentials.access_token):
            raise TypeError(
                "credentials.access_token is async; use the client's async form"
            )
        self._credentials = credentials
        self._origin = httpx.URL(origin)
        # The last token Baseten refused, passed to the next access_token call
        # so the credentials replace it.
        self._refused: str | None = None

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request with the current token."""
        if not _applies_to(request, self._origin):
            yield request
            return
        options = AccessTokenOptions(refused_token=self._refused)
        self._refused = None
        token = _non_empty(self._credentials.access_token(options))
        _set_authorization(request, token)
        response = yield request
        # The request is not sent again, since it may have side effects; only
        # the next request gets a new token.
        if refused_by_baseten(response):
            self._refused = token


class AsyncCredentialsAuth(httpx.Auth):
    """Async form of :class:`CredentialsAuth`."""

    def __init__(self, credentials: AsyncCredentials, *, origin: str) -> None:
        """Create the auth, as :class:`CredentialsAuth` does."""
        if not inspect.iscoroutinefunction(credentials.access_token):
            raise TypeError(
                "credentials.access_token must be async; use the client's sync form"
            )
        self._credentials = credentials
        self._origin = httpx.URL(origin)
        self._refused: str | None = None

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Send the request with the current token."""
        if not _applies_to(request, self._origin):
            yield request
            return
        options = AccessTokenOptions(refused_token=self._refused)
        self._refused = None
        token = _non_empty(await self._credentials.access_token(options))
        _set_authorization(request, token)
        response = yield request
        if refused_by_baseten(response):
            self._refused = token


def refused_by_baseten(response: httpx.Response) -> bool:
    """Report whether Baseten's auth layer refused the request's access token.

    It signals that with this challenge. A model's own error response cannot
    carry it, since its headers are not forwarded.
    """
    if response.status_code != 401:
        return False
    return any(
        challenge.startswith("Bearer ")
        and 'realm="baseten"' in challenge
        and 'error="invalid_token"' in challenge
        for challenge in response.headers.get_list("WWW-Authenticate")
    )


def credentials_origin(
    http_client_override: httpx.Client | httpx.AsyncClient | None,
    base_url: Callable[[], str],
) -> str:
    """Return the origin a client's credentials are sent to.

    The API client sends its requests to *http_client_override*'s base URL
    when it has one, so that is where the token must go; *base_url* is
    called only otherwise, since an inference client given an override may
    have no model or chain to compute one from.
    """
    if http_client_override is not None and http_client_override.base_url.host:
        return str(http_client_override.base_url)
    return base_url()


def _applies_to(request: httpx.Request, origin: httpx.URL) -> bool:
    # Scheme too, so a token meant for https is never sent in the clear.
    return (
        request.url.scheme == origin.scheme
        and request.url.host == origin.host
        and request.url.port == origin.port
        and "Authorization" not in request.headers
    )


def _set_authorization(request: httpx.Request, token: str | None) -> None:
    if token is not None:
        request.headers["Authorization"] = f"Bearer {token}"


def _non_empty(token: AccessToken) -> str:
    if token.token == "":
        raise ValueError("the credentials returned an empty access token")
    return token.token


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

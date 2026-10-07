from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeAlias

import httpx

import baseten.client.managementapi


@dataclass(kw_only=True)
class SandboxTokenProviderContext:
    """What a token provider is told about the token it is asked for."""

    revoked_token: str | None = None
    """A token the server just rejected as revoked.

    Set when the request is being sent again with a new token. Revocation can
    happen well before a token's expiry, for example when any API key of the
    token's user changes.
    """


SandboxTokenProvider: TypeAlias = Callable[[SandboxTokenProviderContext], str]
"""Returns the bearer token to send on a request. Called for every request.

A provider that caches tokens must drop its cached token when it matches
:attr:`SandboxTokenProviderContext.revoked_token`, since the server rejects
that token for good.
"""

AsyncSandboxTokenProvider: TypeAlias = Callable[
    [SandboxTokenProviderContext], Awaitable[str]
]
"""Async form of :data:`SandboxTokenProvider`."""

# Refresh this long before the token's stated expiry, to allow for clock skew
# between this machine and the server, and for time spent in flight.
_TOKEN_EXPIRY_LEEWAY = timedelta(seconds=60)

# A few resends cover a new token being invalidated in the same event as the
# old one. Past that, the rejection is returned, since tokens that keep getting
# rejected point at something a new token cannot fix.
_TOKEN_INVALIDATION_MAX_RETRIES = 2

# Revocation rejects every token issued before a cutoff, the time of the
# revoking event rounded up to the next whole second. A token minted right
# after the event can fall before the cutoff and be rejected too, so after the
# first resend, each one waits this long to be minted past it. A module
# variable so tests can shorten it.
_token_revoked_retry_delay = 1.0

TOKEN_MINT_TIMEOUT = 30.0
"""Seconds that one token exchange may take.

No caller's cancellation reaches a shared exchange, so this keeps a stalled one
from holding up every later caller.
"""


@dataclass(frozen=True)
class _MintedToken:
    """A token minted from an API key, with its expiry."""

    token: str
    expires_at: datetime

    def fresh(self) -> bool:
        """Report whether the token is far enough from expiry to use."""
        return datetime.now(UTC) < self.expires_at - _TOKEN_EXPIRY_LEEWAY


class TokenSource:
    """Where each request's bearer token comes from.

    Either a caller's token provider, or a token minted from an API key and
    cached until shortly before it expires. The API key is exchanged for a
    token because the sandbox APIs do not accept API keys directly. Should that
    change, only this class changes.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: SandboxTokenProvider | None,
        mint_api: baseten.client.managementapi.ApiClient,
    ) -> None:
        """Create a token source. ``mint_api`` authenticates with the API key."""
        if token_provider is not None and api_key != "":
            raise ValueError("api_key must be empty when token_provider is set")
        self._provider = token_provider
        self._mint_api = mint_api if api_key != "" else None
        self._lock = threading.Lock()
        self._cached: _MintedToken | None = None

    def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request.

        ``None`` means no Authorization is sent, which is the advanced opt-out
        of an empty API key with no token provider.
        """
        if self._provider is not None:
            return self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._mint_api is None:
            return None
        # Held across the mint, so a burst on a cold cache sends one exchange.
        with self._lock:
            cached = self._cached
            if cached is not None and cached.fresh():
                return cached.token
            minted = self._mint_api.post_token(
                request=baseten.client.managementapi.CreateTokenRequest(
                    scopes=["sandboxes"]
                )
            )
            self._cached = _MintedToken(
                token=minted.token, expires_at=minted.expires_at
            )
            return minted.token

    def invalidate(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one."""
        with self._lock:
            # Compared by value, so a rejection of an older token never
            # discards a newer one another request already minted.
            if self._cached is not None and self._cached.token == token:
                self._cached = None


class AsyncTokenSource:
    """Async form of :class:`TokenSource`."""

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: AsyncSandboxTokenProvider | None,
        mint_api: baseten.client.managementapi.AsyncApiClient,
    ) -> None:
        """Create a token source. ``mint_api`` authenticates with the API key."""
        if token_provider is not None and api_key != "":
            raise ValueError("api_key must be empty when token_provider is set")
        self._provider = token_provider
        self._mint_api = mint_api if api_key != "" else None
        self._cached: _MintedToken | None = None
        self._minting: asyncio.Task[_MintedToken] | None = None

    async def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request, as :meth:`TokenSource.token` does."""
        if self._provider is not None:
            return await self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._mint_api is None:
            return None
        cached = self._cached
        if cached is not None and cached.fresh():
            return cached.token
        # Every caller shares one mint, so a burst on a cold cache sends one
        # exchange. Shielded, since one caller being cancelled must not cancel
        # the mint the others are waiting on.
        if self._minting is None:
            self._minting = asyncio.create_task(self._mint(self._mint_api))
            # Retrieved here so a failure no caller is left to see is not
            # reported as never retrieved.
            self._minting.add_done_callback(
                lambda task: task.cancelled() or task.exception()
            )
        minted = await asyncio.shield(self._minting)
        return minted.token

    async def invalidate(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one."""
        # Compared by value, so a rejection of an older token never discards a
        # newer one another request already minted.
        if self._cached is not None and self._cached.token == token:
            self._cached = None

    async def _mint(
        self, mint_api: baseten.client.managementapi.AsyncApiClient
    ) -> _MintedToken:
        """Exchange the API key for a token and cache it."""
        try:
            minted = await mint_api.post_token(
                request=baseten.client.managementapi.CreateTokenRequest(
                    scopes=["sandboxes"]
                )
            )
            self._cached = _MintedToken(
                token=minted.token, expires_at=minted.expires_at
            )
            return self._cached
        finally:
            self._minting = None


class TokenAuth(httpx.Auth):
    """Authenticates every request with a token from a :class:`TokenSource`."""

    def __init__(self, tokens: TokenSource) -> None:
        """Create the auth."""
        self.tokens = tokens

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request, again with a new token if its token was revoked."""
        replayable = _is_replayable(request)
        revoked_token: str | None = None
        for retry in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = self.tokens.token(revoked_token)
            _set_authorization(request, token)
            response = yield request
            if token is None or not replayable or not _is_token_revoked(response):
                return
            if retry == _TOKEN_INVALIDATION_MAX_RETRIES:
                return
            self.tokens.invalidate(token)
            revoked_token = token
            if retry > 0:
                time.sleep(_token_revoked_retry_delay)


class AsyncTokenAuth(httpx.Auth):
    """Authenticates every request with a token from an :class:`AsyncTokenSource`."""

    def __init__(self, tokens: AsyncTokenSource) -> None:
        """Create the auth."""
        self.tokens = tokens

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Send the request, again with a new token if its token was revoked."""
        replayable = _is_replayable(request)
        revoked_token: str | None = None
        for retry in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = await self.tokens.token(revoked_token)
            _set_authorization(request, token)
            response = yield request
            if token is None or not replayable or not _is_token_revoked(response):
                return
            if retry == _TOKEN_INVALIDATION_MAX_RETRIES:
                return
            await self.tokens.invalidate(token)
            revoked_token = token
            if retry > 0:
                await asyncio.sleep(_token_revoked_retry_delay)


def _is_replayable(request: httpx.Request) -> bool:
    """Report whether a request's body can be sent again."""
    # A stream body can be read only once, so a request carrying one is never
    # sent again. The SDK itself never sends one; only raw API calls can.
    return isinstance(request.stream, httpx.ByteStream)


def _set_authorization(request: httpx.Request, token: str | None) -> None:
    """Set the request's bearer token, or leave Authorization unset for ``None``."""
    if token is not None:
        request.headers["Authorization"] = f"Bearer {token}"


def _is_token_revoked(response: httpx.Response) -> bool:
    """Report whether the server rejected a request's token as revoked.

    The request was rejected before any work was done, so sending it again
    with a new token is safe even when it has side effects.
    """
    # Both the control plane and sandboxes send this header, while their
    # bodies differ.
    return (
        response.status_code == 401
        and response.headers.get("x-blaxel-error-code") == "TOKEN_REVOKED"
    )

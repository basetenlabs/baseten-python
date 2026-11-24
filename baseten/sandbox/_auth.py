from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeAlias

import httpx

import baseten.client.managementapi
from baseten.client._token_auth import (
    AccessToken,
    AsyncTokenCache,
    TokenCache,
    _set_authorization,
)


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
# between this machine and the server, and for time spent in flight. Capped at
# half the token's lifetime.
_TOKEN_EXPIRY_LEEWAY = timedelta(seconds=60)

# A few retries cover a new token being invalidated in the same event as the
# old one. Past that, the rejection is returned, since tokens that keep getting
# rejected point at something a new token cannot fix.
_TOKEN_INVALIDATION_MAX_RETRIES = 2

# Revocation rejects every token issued before a cutoff, the time of the
# revoking event rounded up to the next whole second. A token minted right
# after the event can fall before the cutoff and be rejected too, so after the
# first resend, each one waits this long to be minted past it. A module
# variable so tests can shorten it.
_token_revoked_retry_delay_sec = 1.0

TOKEN_MINT_TIMEOUT = 30.0
"""Seconds that one token exchange may take.

No caller's cancellation reaches a shared exchange, so this keeps a stalled one
from holding up every later caller.
"""

_SANDBOX_TOKEN_REQUEST = baseten.client.managementapi.CreateTokenRequest(
    scopes=["sandboxes"]
)


class TokenSource:
    """Where each request's bearer token comes from.

    Either a caller's token provider, or a token minted through *mint_api* and
    cached until shortly before it expires. *mint_api* authenticates with an
    API key or credentials, which are exchanged for a token because the
    sandbox APIs do not accept them directly. Should that change, only this
    class changes.
    """

    def __init__(
        self,
        *,
        token_provider: SandboxTokenProvider | None,
        mint_api: baseten.client.managementapi.ApiClient | None,
    ) -> None:
        """Create a token source.

        With neither *token_provider* nor *mint_api*, no Authorization is
        sent, which is the advanced opt-out of an empty API key.
        """
        self._provider = token_provider
        self._cache = (
            None
            if token_provider is not None or mint_api is None
            else _token_cache(mint_api)
        )

    def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request, or ``None`` to send no Authorization.

        ``revoked_token`` is the token the previous attempt was refused with,
        if any.
        """
        if self._provider is not None:
            return self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._cache is None:
            return None
        return self._cache.token().token

    def invalidate(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one."""
        if self._cache is not None:
            self._cache.invalidate(token)


class AsyncTokenSource:
    """Async form of :class:`TokenSource`."""

    def __init__(
        self,
        *,
        token_provider: AsyncSandboxTokenProvider | None,
        mint_api: baseten.client.managementapi.AsyncApiClient | None,
    ) -> None:
        """Create a token source, as :class:`TokenSource` does."""
        self._provider = token_provider
        self._cache = (
            None
            if token_provider is not None or mint_api is None
            else _async_token_cache(mint_api)
        )

    async def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request, as :meth:`TokenSource.token` does."""
        if self._provider is not None:
            return await self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._cache is None:
            return None
        return (await self._cache.token()).token

    def invalidate(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one."""
        if self._cache is not None:
            self._cache.invalidate(token)


class TokenAuth(httpx.Auth):
    """Authenticates every request with a token from a :class:`TokenSource`.

    When the server rejects the token as revoked (see :func:`is_token_revoked`),
    the request is sent again with a new one when its body can be, at most
    twice.
    """

    def __init__(self, tokens: TokenSource) -> None:
        """Create the auth."""
        self.tokens = tokens

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request, again with a new token if its token was revoked."""
        replayable = _is_replayable(request)
        revoked: str | None = None
        for retry in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = self.tokens.token(revoked)
            _set_authorization(request, token)
            response = yield request
            if token is None or not is_token_revoked(response):
                return
            # Dropped even when the request is not sent again, so later
            # requests do not reuse it.
            self.tokens.invalidate(token)
            if not replayable or retry == _TOKEN_INVALIDATION_MAX_RETRIES:
                return
            revoked = token
            if retry > 0:
                time.sleep(_token_revoked_retry_delay_sec)


class AsyncTokenAuth(httpx.Auth):
    """Async form of :class:`TokenAuth`."""

    def __init__(self, tokens: AsyncTokenSource) -> None:
        """Create the auth."""
        self.tokens = tokens

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Send the request, again with a new token if its token was revoked."""
        replayable = _is_replayable(request)
        revoked: str | None = None
        for retry in range(_TOKEN_INVALIDATION_MAX_RETRIES + 1):
            token = await self.tokens.token(revoked)
            _set_authorization(request, token)
            response = yield request
            if token is None or not is_token_revoked(response):
                return
            self.tokens.invalidate(token)
            if not replayable or retry == _TOKEN_INVALIDATION_MAX_RETRIES:
                return
            revoked = token
            if retry > 0:
                await asyncio.sleep(_token_revoked_retry_delay_sec)


def is_token_revoked(response: httpx.Response) -> bool:
    """Report whether the server rejected a request's token as revoked.

    It can do so before the token's stated expiry, for example after a role
    change. The request was rejected before any work was done, so sending it
    again with a new token is safe even when it has side effects.
    """
    # Both the control plane and sandboxes send this header, while their
    # bodies differ.
    return (
        response.status_code == 401
        and response.headers.get("x-blaxel-error-code") == "TOKEN_REVOKED"
    )


def _is_replayable(request: httpx.Request) -> bool:
    """Report whether a request's body can be sent again."""
    # A stream body can be read only once, so a request carrying one is never
    # sent again. The SDK itself never sends one; only raw API calls can.
    return isinstance(request.stream, httpx.ByteStream)


def _token_cache(mint_api: baseten.client.managementapi.ApiClient) -> TokenCache:
    return TokenCache(
        mint=lambda: _access_token(mint_api.post_token(request=_SANDBOX_TOKEN_REQUEST)),
        refresh_leeway=_TOKEN_EXPIRY_LEEWAY,
    )


def _async_token_cache(
    mint_api: baseten.client.managementapi.AsyncApiClient,
) -> AsyncTokenCache:
    async def mint() -> AccessToken:
        return _access_token(await mint_api.post_token(request=_SANDBOX_TOKEN_REQUEST))

    return AsyncTokenCache(mint=mint, refresh_leeway=_TOKEN_EXPIRY_LEEWAY)


def _access_token(minted: baseten.client.managementapi.Token) -> AccessToken:
    return AccessToken(token=minted.token, expires_at=minted.expires_at)

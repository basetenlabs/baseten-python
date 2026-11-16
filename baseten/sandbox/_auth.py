from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeAlias

import baseten.client._token_auth
import baseten.client.managementapi
from baseten.client._token_auth import AccessToken, AsyncTokenCache, TokenCache


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
        self._cache = (
            TokenCache(
                mint=lambda: _access_token(
                    mint_api.post_token(request=_SANDBOX_TOKEN_REQUEST)
                ),
                refresh_before=_TOKEN_EXPIRY_LEEWAY,
            )
            if api_key != ""
            else None
        )

    def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request.

        ``revoked_token`` is the token the previous attempt was refused with,
        if any. ``None`` means no Authorization is sent, which is the advanced
        opt-out of an empty API key with no token provider.
        """
        if self._provider is not None:
            return self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._cache is None:
            return None
        if revoked_token is not None:
            self.invalidate(revoked_token)
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
        api_key: str,
        token_provider: AsyncSandboxTokenProvider | None,
        mint_api: baseten.client.managementapi.AsyncApiClient,
    ) -> None:
        """Create a token source. ``mint_api`` authenticates with the API key."""
        if token_provider is not None and api_key != "":
            raise ValueError("api_key must be empty when token_provider is set")
        self._provider = token_provider

        async def mint() -> AccessToken:
            return _access_token(
                await mint_api.post_token(request=_SANDBOX_TOKEN_REQUEST)
            )

        self._cache = (
            AsyncTokenCache(mint=mint, refresh_before=_TOKEN_EXPIRY_LEEWAY)
            if api_key != ""
            else None
        )

    async def token(self, revoked_token: str | None = None) -> str | None:
        """Return the token for a request, as :meth:`TokenSource.token` does."""
        if self._provider is not None:
            return await self._provider(
                SandboxTokenProviderContext(revoked_token=revoked_token)
            )
        if self._cache is None:
            return None
        if revoked_token is not None:
            await self.invalidate(revoked_token)
        return (await self._cache.token()).token

    async def invalidate(self, token: str) -> None:
        """Drop a token the server rejected, so the next request gets a new one."""
        if self._cache is not None:
            self._cache.invalidate(token)


class TokenAuth(baseten.client._token_auth.TokenAuth):
    """Authenticates every request with a token from a :class:`TokenSource`."""

    def __init__(self, tokens: TokenSource) -> None:
        """Create the auth."""
        super().__init__(tokens.token)
        self.tokens = tokens


class AsyncTokenAuth(baseten.client._token_auth.AsyncTokenAuth):
    """Authenticates every request with a token from an :class:`AsyncTokenSource`."""

    def __init__(self, tokens: AsyncTokenSource) -> None:
        """Create the auth."""
        super().__init__(tokens.token)
        self.tokens = tokens


def _access_token(minted: baseten.client.managementapi.Token) -> AccessToken:
    return AccessToken(token=minted.token, expires_at=minted.expires_at)

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from baseten.client._management import ManagementClient
from baseten.client._token_auth import (
    AccessToken,
    AccessTokenOptions,
    AsyncTokenCache,
    TokenCache,
    _utc_now,
)
from baseten.client._user_agent import with_user_agent
from baseten.client.managementapi import (
    ApiClient,
    AsyncApiClient,
    ResponseError,
    TokenExchangeRequest,
    TokenExchangeResponse,
)

# Sends a request as is, so the exchange never picks up an HTTP client's own
# auth, such as another client's credentials.
_NO_AUTH = httpx.Auth()

# The exchange takes an RFC 8693 request naming the trust rule as its audience.
_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
# As in the Go SDK. httpx applies it to each phase of the request.
_EXCHANGE_TIMEOUT = httpx.Timeout(10)


@dataclass(frozen=True, kw_only=True)
class IdentityTokenProviderContext:
    """Passed to an :data:`IdentityTokenProvider`.

    Empty for now; fields may be added in later versions.
    """


IdentityTokenProvider: TypeAlias = Callable[[IdentityTokenProviderContext], str]
"""Returns the identity provider's JWT. Called on every exchange."""

AsyncIdentityTokenProvider: TypeAlias = Callable[
    [IdentityTokenProviderContext], Awaitable[str]
]
"""Async form of :data:`IdentityTokenProvider`."""


class _Exchange:
    """The parts of an exchange shared by the sync and async credentials."""

    def __init__(
        self,
        *,
        trust_rule_id: str,
        identity_token_file: str | os.PathLike[str] | None,
        has_identity_token: bool,
        base_url_override: str | None,
        refresh_leeway: timedelta,
    ) -> None:
        if (identity_token_file is None) == (not has_identity_token):
            raise ValueError(
                "exactly one of identity_token_file and identity_token is required"
            )
        if identity_token_file is not None and os.fspath(identity_token_file) == "":
            raise ValueError("identity_token_file must not be empty")
        if not trust_rule_id:
            raise ValueError("trust_rule_id is required")
        if refresh_leeway < timedelta(0):
            raise ValueError("refresh_leeway must not be negative")
        self.trust_rule_id = trust_rule_id
        self.identity_token_file = (
            None if identity_token_file is None else Path(identity_token_file)
        )
        base_url = (base_url_override or ManagementClient.default_base_url()).rstrip(
            "/"
        )
        self.base_url = base_url
        self.headers = with_user_agent({})

    def read_identity_token_file(self) -> str:
        assert self.identity_token_file is not None
        # Re-read on every exchange, since such files are rotated in place.
        token = self.identity_token_file.read_text().strip()
        if token == "":
            raise ValueError(f"identity token file {self.identity_token_file} is empty")
        return token

    def request(self, identity_token: str) -> TokenExchangeRequest:
        token = identity_token.strip()
        if token == "":
            raise ValueError("the identity token provider returned an empty token")
        return TokenExchangeRequest(
            grant_type=_TOKEN_EXCHANGE_GRANT_TYPE,
            subject_token=token,
            subject_token_type=_JWT_TOKEN_TYPE,
            audience=self.trust_rule_id,
        )

    def api_kwargs(self) -> dict[str, Any]:
        # The identity token in the body is what authenticates.
        return {
            "base_url": self.base_url,
            "headers": self.headers,
            "timeout": _EXCHANGE_TIMEOUT,
            "auth": _NO_AUTH,
        }

    def add_context(self, error: Exception) -> None:
        error.add_note(
            f"while exchanging an identity token under trust rule {self.trust_rule_id}"
        )
        if (
            isinstance(error, ResponseError)
            and _oauth_error(error.body) == "invalid_grant"
        ):
            # The server sends every refusal as the same invalid_grant, so the
            # note covers each possible cause.
            error.add_note(
                "workload identity token exchange refused for trust rule "
                f"{self.trust_rule_id} (the trust rule may not exist or may "
                "not match the identity token's issuer, audience, or claims, "
                "or the identity token may have expired)"
            )

    def parse(
        self, response: TokenExchangeResponse, *, issued_at: datetime
    ) -> AccessToken:
        if response.access_token == "":
            raise ValueError(
                "workload identity token exchange: the response carried no access_token"
            )
        if response.token_type.lower() != "bearer":
            raise ValueError(
                "workload identity token exchange: unsupported token_type "
                f"{response.token_type!r}"
            )
        if response.expires_in <= 0:
            raise ValueError(
                f"workload identity token exchange: invalid expires_in {response.expires_in!r}"
            )
        return AccessToken(
            token=response.access_token,
            expires_at=issued_at + timedelta(seconds=response.expires_in),
        )


def _oauth_error(body: str) -> object:
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    return decoded.get("error") if isinstance(decoded, dict) else None


class WorkloadIdentityCredentials:
    """Exchanges an identity provider's JWT for Baseten access tokens.

    OIDC workload identity federation: instead of an API key, a workload
    exchanges its identity provider's token (for example a projected
    Kubernetes service account token) for a short-lived Baseten access token
    under a trust rule. Pass the credentials to a client as ``credentials``::

        from baseten.client import ManagementClient, WorkloadIdentityCredentials

        credentials = WorkloadIdentityCredentials(
            trust_rule_id="my-trust-rule-id",
            identity_token_file="/var/run/secrets/tokens/baseten",
        )
        with ManagementClient(credentials=credentials) as client:
            print(client.api.get_organizations_me().org_id)

    The exchange happens on first use; the access token is cached in memory
    and replaced shortly before it expires. Safe for use from several threads;
    concurrent callers share one exchange. Everything is passed explicitly; no
    environment variables are read. A refused exchange raises
    :class:`baseten.client.managementapi.ResponseError`. Only the exchange's
    HTTP request has a timeout, of 10 seconds; *identity_token* and reading
    *identity_token_file* have none, so a caller that needs the whole call
    bounded sets its own deadline.
    """

    def __init__(
        self,
        *,
        trust_rule_id: str,
        identity_token_file: str | os.PathLike[str] | None = None,
        identity_token: IdentityTokenProvider | None = None,
        base_url_override: str | None = None,
        refresh_leeway: timedelta = timedelta(minutes=5),
        http_client_override: httpx.Client | None = None,
    ) -> None:
        """Validate the options. Baseten is not contacted until first use.

        Args:
            trust_rule_id: The Baseten trust rule the identity token is
                exchanged under.
            identity_token_file: A file holding the identity provider's JWT,
                such as a projected Kubernetes service account token. Re-read
                on every exchange, since such files are rotated in place.
                Exactly one of *identity_token_file* and *identity_token* is
                required.
            identity_token: Returns the identity provider's JWT. Called on
                every exchange with an :class:`IdentityTokenProviderContext`.
            base_url_override: Management API base URL the exchange is sent
                to. ``None`` uses :meth:`ManagementClient.default_base_url`.
                Must point at the same Baseten environment as the clients
                given these credentials, since a token is accepted only by
                the environment that issued it.
            refresh_leeway: How long before expiry the access token is
                replaced, capped at half the token's lifetime so a short-lived
                token is still reused.
            http_client_override: HTTP client to send the exchange with.
                ``None`` uses a new one per exchange. It should not follow
                redirects, since the request body carries the identity token.
        """
        self._exchange = _Exchange(
            trust_rule_id=trust_rule_id,
            identity_token_file=identity_token_file,
            has_identity_token=identity_token is not None,
            base_url_override=base_url_override,
            refresh_leeway=refresh_leeway,
        )
        self._identity_token = identity_token
        self._http_client = http_client_override
        # Replaced in tests.
        self._now: Callable[[], datetime] = _utc_now
        self._cache = TokenCache(
            mint=self._exchange_token,
            refresh_leeway=refresh_leeway,
            now=lambda: self._now(),
        )

    def access_token(self, options: AccessTokenOptions) -> AccessToken:
        """Return a current access token, exchanging for a new one when due."""
        if options.refused_token is not None:
            self._cache.invalidate(options.refused_token)
        return self._cache.token()

    def _exchange_token(self) -> AccessToken:
        try:
            if self._identity_token is not None:
                identity_token = self._identity_token(IdentityTokenProviderContext())
            else:
                identity_token = self._exchange.read_identity_token_file()
            request = self._exchange.request(identity_token)
            # Taken before sending, so the computed expiry errs early.
            issued_at = self._now()
            if self._http_client is not None:
                response = ApiClient(
                    self._http_client, **self._exchange.api_kwargs()
                ).post_auth_token(request=request)
            else:
                with httpx.Client() as http_client:
                    response = ApiClient(
                        http_client, **self._exchange.api_kwargs()
                    ).post_auth_token(request=request)
            return self._exchange.parse(response, issued_at=issued_at)
        except Exception as e:
            self._exchange.add_context(e)
            raise


class AsyncWorkloadIdentityCredentials:
    """Async form of :class:`WorkloadIdentityCredentials`, for async clients.

    Concurrent tasks share one exchange, and one task being cancelled does not
    cancel the exchange the others wait on.
    """

    def __init__(
        self,
        *,
        trust_rule_id: str,
        identity_token_file: str | os.PathLike[str] | None = None,
        identity_token: AsyncIdentityTokenProvider | None = None,
        base_url_override: str | None = None,
        refresh_leeway: timedelta = timedelta(minutes=5),
        http_client_override: httpx.AsyncClient | None = None,
    ) -> None:
        """Validate the options, as :class:`WorkloadIdentityCredentials` does."""
        self._exchange = _Exchange(
            trust_rule_id=trust_rule_id,
            identity_token_file=identity_token_file,
            has_identity_token=identity_token is not None,
            base_url_override=base_url_override,
            refresh_leeway=refresh_leeway,
        )
        self._identity_token = identity_token
        self._http_client = http_client_override
        # Replaced in tests.
        self._now: Callable[[], datetime] = _utc_now
        self._cache = AsyncTokenCache(
            mint=self._exchange_token,
            refresh_leeway=refresh_leeway,
            now=lambda: self._now(),
        )

    async def access_token(self, options: AccessTokenOptions) -> AccessToken:
        """Return a current access token, exchanging for a new one when due."""
        if options.refused_token is not None:
            self._cache.invalidate(options.refused_token)
        return await self._cache.token()

    async def _exchange_token(self) -> AccessToken:
        try:
            if self._identity_token is not None:
                identity_token = await self._identity_token(
                    IdentityTokenProviderContext()
                )
            else:
                # Off the event loop, since the file system can be slow.
                identity_token = await asyncio.to_thread(
                    self._exchange.read_identity_token_file
                )
            request = self._exchange.request(identity_token)
            # Taken before sending, so the computed expiry errs early.
            issued_at = self._now()
            if self._http_client is not None:
                response = await AsyncApiClient(
                    self._http_client, **self._exchange.api_kwargs()
                ).post_auth_token(request=request)
            else:
                async with httpx.AsyncClient() as http_client:
                    response = await AsyncApiClient(
                        http_client, **self._exchange.api_kwargs()
                    ).post_auth_token(request=request)
            return self._exchange.parse(response, issued_at=issued_at)
        except Exception as e:
            self._exchange.add_context(e)
            raise

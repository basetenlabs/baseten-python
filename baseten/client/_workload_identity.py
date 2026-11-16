from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeAlias

import httpx

from baseten.client._token_auth import (
    AccessToken,
    AsyncTokenCache,
    TokenCache,
    _utc_now,
)
from baseten.client._user_agent import with_user_agent

# The exchange takes an RFC 8693 request naming the trust rule as its audience.
_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"

_DEFAULT_BASE_URL = "https://api.baseten.co"

IdentityTokenProvider: TypeAlias = Callable[[], str]
"""Returns the identity provider's JWT. Called on every exchange."""

AsyncIdentityTokenProvider: TypeAlias = Callable[[], Awaitable[str]]
"""Async form of :data:`IdentityTokenProvider`."""


class WorkloadIdentityError(Exception):
    """A workload identity token exchange failed."""


class InvalidGrantError(WorkloadIdentityError):
    """Baseten refused the identity token under the trust rule.

    The server gives no reason: the trust rule may not exist, may not match the
    identity token's issuer or claims, or the identity token may be expired or
    have the wrong audience.
    """


class _Exchange:
    """The parts of an exchange shared by the sync and async credentials."""

    def __init__(
        self,
        *,
        trust_rule_id: str,
        identity_token_file: str | os.PathLike[str] | None,
        has_identity_token: bool,
        base_url_override: str | None,
        refresh_before_expiry: timedelta,
        exchange_timeout_sec: float,
    ) -> None:
        if (identity_token_file is None) == (not has_identity_token):
            raise ValueError(
                "exactly one of identity_token_file and identity_token is required"
            )
        if trust_rule_id == "":
            raise ValueError("trust_rule_id is required")
        if refresh_before_expiry < timedelta(0):
            raise ValueError("refresh_before_expiry must not be negative")
        if exchange_timeout_sec <= 0:
            raise ValueError("exchange_timeout_sec must be positive")
        self.trust_rule_id = trust_rule_id
        self.identity_token_file = (
            None if identity_token_file is None else Path(identity_token_file)
        )
        base_url = (base_url_override or _DEFAULT_BASE_URL).rstrip("/")
        self.url = f"{base_url}/v1/auth/token"
        self.timeout = httpx.Timeout(exchange_timeout_sec)
        self.headers = with_user_agent({"Accept": "application/json"})

    def read_identity_token_file(self) -> str:
        assert self.identity_token_file is not None
        # Re-read on every exchange, since such files are rotated in place.
        token = self.identity_token_file.read_text().strip()
        if token == "":
            raise WorkloadIdentityError(
                f"identity token file {self.identity_token_file} is empty"
            )
        return token

    def body(self, identity_token: str) -> dict[str, str]:
        token = identity_token.strip()
        if token == "":
            raise WorkloadIdentityError(
                "the identity token provider returned an empty token"
            )
        return {
            "grant_type": _TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": token,
            "subject_token_type": _JWT_TOKEN_TYPE,
            "audience": self.trust_rule_id,
        }

    def parse(self, response: httpx.Response, *, issued_at: datetime) -> AccessToken:
        try:
            body = response.json()
        except ValueError:
            raise WorkloadIdentityError(
                f"workload identity token exchange: HTTP {response.status_code} "
                f"with a non-JSON response: {_truncate(response.text)}"
            ) from None
        if not isinstance(body, dict):
            raise WorkloadIdentityError(
                f"workload identity token exchange: HTTP {response.status_code} "
                f"with an unexpected response: {_truncate(response.text)}"
            )
        if response.status_code != 200:
            if body.get("error") == "invalid_grant":
                raise InvalidGrantError(
                    "workload identity token exchange refused for trust rule "
                    f"{self.trust_rule_id}: invalid_grant (the trust rule may not "
                    "exist or may not match the identity token's issuer, "
                    "audience, or claims, or the identity token may have expired)"
                )
            raise WorkloadIdentityError(
                f"workload identity token exchange: HTTP {response.status_code}: "
                f"{_truncate(response.text)}"
            )
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or access_token == "":
            raise WorkloadIdentityError(
                "workload identity token exchange: the response carried no access_token"
            )
        token_type = body.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise WorkloadIdentityError(
                "workload identity token exchange: unsupported token_type "
                f"{token_type!r}"
            )
        expires_in = body.get("expires_in")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise WorkloadIdentityError(
                f"workload identity token exchange: invalid expires_in {expires_in!r}"
            )
        return AccessToken(
            token=access_token, expires_at=issued_at + timedelta(seconds=expires_in)
        )


class WorkloadIdentityCredentials:
    """Exchanges an identity provider's JWT for a Baseten access token.

    OIDC workload identity federation: the identity token is exchanged under a
    Baseten trust rule on first use, the access token is cached in memory and
    replaced shortly before it expires. Safe for use from several threads;
    concurrent callers share one exchange. Pass to a client as ``credentials``
    in place of an API key. Everything is passed explicitly; no environment
    variables are read.
    """

    def __init__(
        self,
        *,
        trust_rule_id: str,
        identity_token_file: str | os.PathLike[str] | None = None,
        identity_token: IdentityTokenProvider | None = None,
        base_url_override: str | None = None,
        refresh_before_expiry: timedelta = timedelta(minutes=5),
        exchange_timeout_sec: float = 10.0,
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
                every exchange.
            base_url_override: Management API base URL the exchange is sent
                to. ``None`` uses ``https://api.baseten.co``.
            refresh_before_expiry: How long before expiry the access token is
                replaced, capped at half the token's lifetime so a short-lived
                token is still reused.
            exchange_timeout_sec: Bounds each phase of one exchange, which
                every caller waiting for a token shares.
            http_client_override: HTTP client to send the exchange with.
                ``None`` uses a new one per exchange.
        """
        self._exchange = _Exchange(
            trust_rule_id=trust_rule_id,
            identity_token_file=identity_token_file,
            has_identity_token=identity_token is not None,
            base_url_override=base_url_override,
            refresh_before_expiry=refresh_before_expiry,
            exchange_timeout_sec=exchange_timeout_sec,
        )
        self._identity_token = identity_token
        self._http_client = http_client_override
        # Replaced in tests.
        self._now: Callable[[], datetime] = _utc_now
        self._cache = TokenCache(
            mint=self._exchange_token,
            refresh_before=refresh_before_expiry,
            now=lambda: self._now(),
        )

    def access_token(self) -> AccessToken:
        """Return a current access token, exchanging for a new one when due."""
        return self._cache.token()

    def invalidate_access_token(self, token: str) -> None:
        """Drop *token* if it is still cached, so the next call exchanges again."""
        self._cache.invalidate(token)

    def _exchange_token(self) -> AccessToken:
        if self._identity_token is not None:
            identity_token = self._identity_token()
        else:
            identity_token = self._exchange.read_identity_token_file()
        body = self._exchange.body(identity_token)
        # Taken before sending, so the computed expiry errs early.
        issued_at = self._now()
        try:
            if self._http_client is not None:
                response = self._post(self._http_client, body)
            else:
                with httpx.Client() as http_client:
                    response = self._post(http_client, body)
        except httpx.HTTPError as e:
            raise WorkloadIdentityError(f"workload identity token exchange: {e}") from e
        return self._exchange.parse(response, issued_at=issued_at)

    def _post(self, http_client: httpx.Client, body: dict[str, str]) -> httpx.Response:
        return http_client.post(
            self._exchange.url,
            json=body,
            headers=self._exchange.headers,
            timeout=self._exchange.timeout,
        )


class AsyncWorkloadIdentityCredentials:
    """Async form of :class:`WorkloadIdentityCredentials`.

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
        refresh_before_expiry: timedelta = timedelta(minutes=5),
        exchange_timeout_sec: float = 10.0,
        http_client_override: httpx.AsyncClient | None = None,
    ) -> None:
        """Validate the options, as :class:`WorkloadIdentityCredentials` does.

        Here *exchange_timeout_sec* bounds the whole exchange, including
        *identity_token*.
        """
        self._exchange = _Exchange(
            trust_rule_id=trust_rule_id,
            identity_token_file=identity_token_file,
            has_identity_token=identity_token is not None,
            base_url_override=base_url_override,
            refresh_before_expiry=refresh_before_expiry,
            exchange_timeout_sec=exchange_timeout_sec,
        )
        self._identity_token = identity_token
        self._http_client = http_client_override
        self._exchange_timeout_sec = exchange_timeout_sec
        # Replaced in tests.
        self._now: Callable[[], datetime] = _utc_now
        self._cache = AsyncTokenCache(
            mint=self._exchange_token,
            refresh_before=refresh_before_expiry,
            now=lambda: self._now(),
        )

    async def access_token(self) -> AccessToken:
        """Return a current access token, exchanging for a new one when due."""
        return await self._cache.token()

    def invalidate_access_token(self, token: str) -> None:
        """Drop *token* if it is still cached, so the next call exchanges again."""
        self._cache.invalidate(token)

    async def _exchange_token(self) -> AccessToken:
        try:
            async with asyncio.timeout(self._exchange_timeout_sec):
                return await self._exchange_once()
        except TimeoutError as e:
            raise WorkloadIdentityError(
                "workload identity token exchange timed out after "
                f"{self._exchange_timeout_sec}s"
            ) from e

    async def _exchange_once(self) -> AccessToken:
        if self._identity_token is not None:
            identity_token = await self._identity_token()
        else:
            identity_token = self._exchange.read_identity_token_file()
        body = self._exchange.body(identity_token)
        # Taken before sending, so the computed expiry errs early.
        issued_at = self._now()
        try:
            if self._http_client is not None:
                response = await self._post(self._http_client, body)
            else:
                async with httpx.AsyncClient() as http_client:
                    response = await self._post(http_client, body)
        except httpx.HTTPError as e:
            raise WorkloadIdentityError(f"workload identity token exchange: {e}") from e
        return self._exchange.parse(response, issued_at=issued_at)

    async def _post(
        self, http_client: httpx.AsyncClient, body: dict[str, str]
    ) -> httpx.Response:
        return await http_client.post(
            self._exchange.url,
            json=body,
            headers=self._exchange.headers,
            timeout=self._exchange.timeout,
        )


def _truncate(text: str, max_len: int = 512) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[:max_len] + "..."

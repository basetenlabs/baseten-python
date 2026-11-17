from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

import httpx

import baseten.client.managementapi
from baseten.client._token_auth import (
    AsyncCredentials,
    AsyncCredentialsAuth,
    Credentials,
    CredentialsAuth,
    check_client_auth,
)
from baseten.client._user_agent import with_user_agent


@dataclass(frozen=True)
class ManagementClientOptions:
    """Options for :class:`ManagementClient` and :class:`AsyncManagementClient`.

    Obtain via :attr:`ManagementClient.options` to inspect the values a client
    was constructed with.
    """

    api_key: str | None
    """API key for authentication, or ``None`` when *credentials* is set."""

    headers: Mapping[str, str] | None = None
    """Additional headers to send on every request."""

    base_url_override: str | None = None
    """Explicit base URL override, or ``None`` to use the default."""

    credentials: Credentials | AsyncCredentials | None = None
    """Supplies short-lived access tokens, used in place of *api_key*."""

    @property
    def base_url(self) -> str:
        """The resolved base URL for the management API."""
        if self.base_url_override is not None:
            return self.base_url_override
        return ManagementClient.default_base_url()


class ManagementClient:
    """Synchronous client for the Baseten Management API.

    Can be used as a context manager to ensure the underlying HTTP client is
    closed on exit.
    """

    @classmethod
    def default_base_url(cls) -> str:
        """Return the default base URL for the management API."""
        return "https://api.baseten.co"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        credentials: Credentials | None = None,
        headers: Mapping[str, str] | None = None,
        base_url_override: str | None = None,
        http_client_override: httpx.Client | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new synchronous management client.

        Args:
            api_key: API key for authentication. Exactly one of *api_key* and
                *credentials* is required.
            credentials: Supplies short-lived access tokens in place of an
                API key, such as :class:`WorkloadIdentityCredentials`. A token
                is fetched for every request, so a long-lived client picks up
                refreshed tokens. It is sent only to this client's API host,
                and each request is sent once.
            headers: Additional headers to send on every request.
            base_url_override: Override the default base URL. When ``None``,
                :meth:`default_base_url` is used.
            http_client_override: Pre-configured httpx client. When provided,
                the caller is responsible for setting base URL and all
                headers.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
        """
        check_client_auth(api_key=api_key, credentials=credentials, headers=headers)
        self._options = ManagementClientOptions(
            api_key=api_key,
            headers=headers,
            base_url_override=base_url_override,
            credentials=credentials,
        )
        auth = (
            None
            if credentials is None
            else CredentialsAuth(credentials, origin=self._options.base_url)
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty api_key is an advanced opt-out from sending Authorization.
            if api_key:
                request_headers["Authorization"] = f"Bearer {api_key}"
            # No read timeout, matching the JS and Go clients.
            self._http_client = httpx.Client(
                base_url=self._options.base_url,
                headers=with_user_agent(request_headers),
                timeout=httpx.Timeout(5.0, read=None),
                auth=auth,
            )
            self.close_http_client_on_close = (
                True
                if close_http_client_on_close is None
                else close_http_client_on_close
            )
        else:
            self._http_client = http_client_override
            self.close_http_client_on_close = (
                False
                if close_http_client_on_close is None
                else close_http_client_on_close
            )
        self._api = baseten.client.managementapi.ApiClient(self._http_client, auth=auth)

    @property
    def options(self) -> ManagementClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.Client:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.managementapi.ApiClient:
        """The generated API client.

        The generated API surface is not covered by stability guarantees and
        may change between versions.
        """
        return self._api

    def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            self._http_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncManagementClient:
    """Asynchronous client for the Baseten Management API.

    Can be used as an async context manager to ensure the underlying HTTP
    client is closed on exit.
    """

    @classmethod
    def default_base_url(cls) -> str:
        """Return the default base URL for the management API."""
        return ManagementClient.default_base_url()

    def __init__(
        self,
        *,
        api_key: str | None = None,
        credentials: AsyncCredentials | None = None,
        headers: Mapping[str, str] | None = None,
        base_url_override: str | None = None,
        http_client_override: httpx.AsyncClient | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new asynchronous management client.

        Args:
            api_key: API key for authentication. Exactly one of *api_key* and
                *credentials* is required.
            credentials: Supplies short-lived access tokens, as for
                :class:`ManagementClient`.
            headers: Additional headers to send on every request.
            base_url_override: Override the default base URL. When ``None``,
                :meth:`default_base_url` is used.
            http_client_override: Pre-configured httpx async client. When
                provided, the caller is responsible for setting base URL
                and all headers.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
        """
        check_client_auth(api_key=api_key, credentials=credentials, headers=headers)
        self._options = ManagementClientOptions(
            api_key=api_key,
            headers=headers,
            base_url_override=base_url_override,
            credentials=credentials,
        )
        auth = (
            None
            if credentials is None
            else AsyncCredentialsAuth(credentials, origin=self._options.base_url)
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty api_key is an advanced opt-out from sending Authorization.
            if api_key:
                request_headers["Authorization"] = f"Bearer {api_key}"
            # No read timeout, matching the JS and Go clients.
            self._http_client = httpx.AsyncClient(
                base_url=self._options.base_url,
                headers=with_user_agent(request_headers),
                timeout=httpx.Timeout(5.0, read=None),
                auth=auth,
            )
            self.close_http_client_on_close = (
                True
                if close_http_client_on_close is None
                else close_http_client_on_close
            )
        else:
            self._http_client = http_client_override
            self.close_http_client_on_close = (
                False
                if close_http_client_on_close is None
                else close_http_client_on_close
            )
        self._api = baseten.client.managementapi.AsyncApiClient(
            self._http_client, auth=auth
        )

    @property
    def options(self) -> ManagementClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.managementapi.AsyncApiClient:
        """The generated API client.

        The generated API surface is not covered by stability guarantees and
        may change between versions.
        """
        return self._api

    async def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

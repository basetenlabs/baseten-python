from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

import httpx

import baseten.client.sandboxapi
from baseten.client._user_agent import with_user_agent


@dataclass(frozen=True)
class SandboxClientOptions:
    """Options for :class:`SandboxClient` and :class:`AsyncSandboxClient`.

    Obtain via :attr:`SandboxClient.options` to inspect the values a client
    was constructed with.
    """

    token: str
    """Sandbox token for authentication."""

    base_url: str
    """Base URL of the sandbox, without a trailing slash.

    Each sandbox is reached at its own host, so there is no default.
    """

    headers: Mapping[str, str] | None = None
    """Additional headers to send on every request."""


class SandboxClient:
    """Synchronous client for one sandbox's execution-plane API.

    Can be used as a context manager to ensure the underlying HTTP client is
    closed on exit.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        http_client_override: httpx.Client | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new synchronous sandbox client.

        Args:
            token: Sandbox token for authentication.
            base_url: Base URL of the sandbox, without a trailing slash.
            headers: Additional headers to send on every request.
            http_client_override: Pre-configured httpx client. When provided,
                the caller is responsible for setting base URL and all
                headers.
            close_http_client_on_close: Whether :meth:`close` should close the
                underlying HTTP client. Defaults to ``True`` when the client
                is created internally, ``False`` when *http_client_override*
                is provided.
        """
        self._options = SandboxClientOptions(
            token=token, base_url=base_url, headers=headers
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty token is an advanced opt-out from sending Authorization.
            if token != "":
                request_headers["Authorization"] = f"Bearer {token}"
            self._http_client = httpx.Client(
                base_url=base_url,
                headers=with_user_agent(request_headers),
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
        self._api = baseten.client.sandboxapi.ApiClient(self._http_client)

    @property
    def options(self) -> SandboxClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.Client:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.sandboxapi.ApiClient:
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


class AsyncSandboxClient:
    """Asynchronous client for one sandbox's execution-plane API.

    Can be used as an async context manager to ensure the underlying HTTP
    client is closed on exit.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        http_client_override: httpx.AsyncClient | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new asynchronous sandbox client.

        Args:
            token: Sandbox token for authentication.
            base_url: Base URL of the sandbox, without a trailing slash.
            headers: Additional headers to send on every request.
            http_client_override: Pre-configured httpx client. When provided,
                the caller is responsible for setting base URL and all
                headers.
            close_http_client_on_close: Whether :meth:`close` should close the
                underlying HTTP client. Defaults to ``True`` when the client
                is created internally, ``False`` when *http_client_override*
                is provided.
        """
        self._options = SandboxClientOptions(
            token=token, base_url=base_url, headers=headers
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty token is an advanced opt-out from sending Authorization.
            if token != "":
                request_headers["Authorization"] = f"Bearer {token}"
            self._http_client = httpx.AsyncClient(
                base_url=base_url,
                headers=with_user_agent(request_headers),
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
        self._api = baseten.client.sandboxapi.AsyncApiClient(self._http_client)

    @property
    def options(self) -> SandboxClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.sandboxapi.AsyncApiClient:
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

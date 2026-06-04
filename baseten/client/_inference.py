from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

import baseten.client.inferenceapi
from baseten.client._user_agent import with_user_agent


@dataclass(frozen=True)
class InferenceClientOptions:
    """Options for :class:`InferenceClient` and :class:`AsyncInferenceClient`.

    Obtain via :attr:`InferenceClient.options` to inspect the values a client
    was constructed with.
    """

    api_key: str
    """API key for authentication."""

    headers: Mapping[str, str] | None = None
    """Additional headers to send on every request."""

    model_id: str | None = None
    """Model ID. Mutually exclusive with *chain_id*."""

    chain_id: str | None = None
    """Chain ID. Mutually exclusive with *model_id*."""

    environment: str | None = None
    """Environment name for regional routing (e.g. ``"production"``). Affects the base URL hostname."""

    base_url_override: str | None = None
    """Explicit base URL override. When set, *model_id*, *chain_id*, and
    *environment* are ignored."""

    @property
    def base_url(self) -> str:
        """The resolved base URL for the inference API."""
        if self.base_url_override is not None:
            return self.base_url_override
        return InferenceClient.default_base_url(
            model_id=self.model_id,
            chain_id=self.chain_id,
            environment=self.environment,
        )


class InferenceClient:
    """Synchronous client for the Baseten Inference API.

    Can be used as a context manager to ensure the underlying HTTP client is
    closed on exit.
    """

    @classmethod
    def default_base_url(
        cls,
        *,
        model_id: str | None = None,
        chain_id: str | None = None,
        environment: str | None = None,
    ) -> str:
        """Compute the default inference base URL.

        Args:
            model_id: Model ID. Mutually exclusive with *chain_id*.
            chain_id: Chain ID. Mutually exclusive with *model_id*.
            environment: Optional environment name.

        Returns:
            The computed base URL.

        Raises:
            ValueError: If both or neither of *model_id* and *chain_id* are
                provided.
        """
        if (model_id is None) == (chain_id is None):
            raise ValueError("exactly one of model_id or chain_id must be provided")
        prefix = f"model-{model_id}" if model_id is not None else f"chain-{chain_id}"
        if environment is not None:
            return f"https://{prefix}-{environment}.api.baseten.co"
        return f"https://{prefix}.api.baseten.co"

    def __init__(
        self,
        *,
        api_key: str,
        headers: Mapping[str, str] | None = None,
        model_id: str | None = None,
        chain_id: str | None = None,
        environment: str | None = None,
        base_url_override: str | None = None,
        http_client_override: httpx.Client | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new synchronous inference client.

        Args:
            api_key: API key for authentication.
            headers: Additional headers to send on every request.
            model_id: Model ID. Mutually exclusive with *chain_id*.
            chain_id: Chain ID. Mutually exclusive with *model_id*.
            environment: Environment name for regional routing (e.g. ``"production"``).
            base_url_override: Override the computed base URL. When set,
                *model_id*, *chain_id*, and *environment* are ignored.
            http_client_override: Pre-configured httpx client. When provided,
                the caller is responsible for setting base URL and all
                headers.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
        """
        self._options = InferenceClientOptions(
            api_key=api_key,
            headers=headers,
            model_id=model_id,
            chain_id=chain_id,
            environment=environment,
            base_url_override=base_url_override,
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty api_key is an advanced opt-out from sending Authorization.
            if api_key != "":
                request_headers["Authorization"] = f"Bearer {api_key}"
            self._http_client = httpx.Client(
                base_url=self._options.base_url,
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
        self._api = baseten.client.inferenceapi.ApiClient(self._http_client)

    @property
    def options(self) -> InferenceClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.Client:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.inferenceapi.ApiClient:
        """The generated API client.

        The generated API surface is not covered by stability guarantees and
        may change between versions.
        """
        return self._api

    def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            self._http_client.close()

    def __enter__(self) -> InferenceClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncInferenceClient:
    """Asynchronous client for the Baseten Inference API.

    Can be used as an async context manager to ensure the underlying HTTP
    client is closed on exit.
    """

    @classmethod
    def default_base_url(
        cls,
        *,
        model_id: str | None = None,
        chain_id: str | None = None,
        environment: str | None = None,
    ) -> str:
        """Compute the default inference base URL.

        See :meth:`InferenceClient.default_base_url` for details.
        """
        return InferenceClient.default_base_url(
            model_id=model_id, chain_id=chain_id, environment=environment
        )

    def __init__(
        self,
        *,
        api_key: str,
        headers: Mapping[str, str] | None = None,
        model_id: str | None = None,
        chain_id: str | None = None,
        environment: str | None = None,
        base_url_override: str | None = None,
        http_client_override: httpx.AsyncClient | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new asynchronous inference client.

        Args:
            api_key: API key for authentication.
            headers: Additional headers to send on every request.
            model_id: Model ID. Mutually exclusive with *chain_id*.
            chain_id: Chain ID. Mutually exclusive with *model_id*.
            environment: Environment name for regional routing (e.g. ``"production"``).
            base_url_override: Override the computed base URL. When set,
                *model_id*, *chain_id*, and *environment* are ignored.
            http_client_override: Pre-configured httpx async client. When
                provided, the caller is responsible for setting base URL
                and all headers.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
        """
        self._options = InferenceClientOptions(
            api_key=api_key,
            headers=headers,
            model_id=model_id,
            chain_id=chain_id,
            environment=environment,
            base_url_override=base_url_override,
        )
        if http_client_override is None:
            request_headers: dict[str, str] = {**(headers or {})}
            # Empty api_key is an advanced opt-out from sending Authorization.
            if api_key != "":
                request_headers["Authorization"] = f"Bearer {api_key}"
            self._http_client = httpx.AsyncClient(
                base_url=self._options.base_url,
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
        self._api = baseten.client.inferenceapi.AsyncApiClient(self._http_client)

    @property
    def options(self) -> InferenceClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def api(self) -> baseten.client.inferenceapi.AsyncApiClient:
        """The generated API client.

        The generated API surface is not covered by stability guarantees and
        may change between versions.
        """
        return self._api

    async def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            await self._http_client.aclose()

    async def __aenter__(self) -> AsyncInferenceClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

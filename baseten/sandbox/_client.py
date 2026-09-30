from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

import httpx

import baseten.client.managementapi
from baseten.client._user_agent import with_user_agent
from baseten.sandbox._auth import (
    AsyncSandboxTokenProvider,
    AsyncTokenSource,
    SandboxTokenProvider,
    SyncTokenSource,
    _AsyncAuthTransport,
    _SyncAuthTransport,
    resolve_http2_flag,
)
from baseten.sandbox._errors import to_sandbox_api_error
from baseten.sandbox._info import (
    SandboxEnvValue,
    SandboxInfo,
    sandbox_envs_to_api,
    sandbox_info_from_api,
)
from baseten.sandbox._retry import SandboxRetryOptions
from baseten.sandbox._sandbox import AsyncSandbox, Sandbox

DEFAULT_MANAGEMENT_BASE_URL = "https://api.baseten.co"

# Only connect time is bounded: a sandbox call runs as long as the operation
# needs. Callers pass a stricter `timeout` when wanted.
DEFAULT_TIMEOUT = httpx.Timeout(None, connect=10.0)


@dataclass(frozen=True)
class SandboxClientOptions:
    """Options for :class:`SandboxClient` and :class:`AsyncSandboxClient`.

    Obtain via :attr:`SandboxClient.options` to inspect the values a client
    was constructed with.
    """

    api_key: str
    """API key for authentication. Empty, together with *token_provider*, is
    the advanced opt-out of API key authentication."""

    token_provider: SandboxTokenProvider | AsyncSandboxTokenProvider | None = None
    """Returns the bearer token for each request instead of one minted from
    the API key, which must then be empty."""

    team_id: str | None = None
    """Team to act in. Unset uses the caller's only team, and is an error for
    callers in more than one team."""

    base_url_override: str | None = None
    """Explicit management API base URL, or None to use the default."""

    headers: Mapping[str, str] | None = None
    """Additional headers to send on every request."""

    http2: bool | None = None
    """Use HTTP/2 when the optional h2 package allows. None enables it when
    h2 is importable, True requires it, False never uses it."""

    timeout: httpx.Timeout | None = None
    """HTTP timeouts. Defaults to bounding only the connect time."""

    retries: SandboxRetryOptions | None = None
    """Retry budgets for transient failures."""

    transport_override: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
    """HTTP transport to send every request through, both planes. For tests
    with a mock transport."""


def _management_base_url(options: SandboxClientOptions) -> str:
    return options.base_url_override or DEFAULT_MANAGEMENT_BASE_URL


def _request_headers(
    options: SandboxClientOptions, authorization: str | None
) -> dict[str, str]:
    headers: dict[str, str] = {**(options.headers or {})}
    if authorization is not None:
        headers["Authorization"] = authorization
    return with_user_agent(headers)


def _sandbox_create_request(
    *,
    name: str | None,
    image: str | None,
    memory: int | None,
    region: str | None,
    envs: list[baseten.client.managementapi.SandboxEnv] | None,
    labels: dict[str, str] | None,
    display_name: str | None,
    external_id: str | None,
) -> baseten.client.managementapi.CreateSandboxRequest:
    """Build the create body from only the fields the caller set.

    The generated client keeps explicitly passed None values, since null can
    mean "clear" on updates. Creation must send nothing instead, so the
    server-side defaults apply.
    """
    create_fields: dict[str, Any] = {"name": name}
    if image is not None:
        create_fields["image"] = image
    if memory is not None:
        create_fields["memory"] = memory
    if region is not None:
        create_fields["region"] = region
    if envs is not None:
        create_fields["envs"] = envs
    if labels is not None:
        create_fields["labels"] = labels
    if display_name is not None:
        create_fields["display_name"] = display_name
    if external_id is not None:
        create_fields["external_id"] = external_id
    return baseten.client.managementapi.CreateSandboxRequest(**create_fields)


class SandboxClient:
    """Synchronous client for Baseten sandboxes.

    Creates, finds, and deletes sandboxes, and hands out :class:`Sandbox`
    objects to work in one. Closing the client closes the sandboxes' shared
    connections. Can be used as a context manager.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: SandboxTokenProvider | None = None,
        team_id: str | None = None,
        base_url_override: str | None = None,
        headers: Mapping[str, str] | None = None,
        http2: bool | None = None,
        timeout: httpx.Timeout | None = None,
        retries: SandboxRetryOptions | None = None,
        transport_override: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a new synchronous sandbox client.

        Every argument matches the :class:`SandboxClientOptions` field of the
        same name.
        """
        self._options = SandboxClientOptions(
            api_key=api_key,
            token_provider=token_provider,
            team_id=team_id,
            base_url_override=base_url_override,
            headers=headers,
            http2=http2,
            timeout=timeout,
            retries=retries,
            transport_override=transport_override,
        )
        if transport_override is None:
            self._pool: httpx.BaseTransport = httpx.HTTPTransport(
                http2=resolve_http2_flag(self._options.http2)
            )
        elif isinstance(transport_override, httpx.BaseTransport):
            # MockTransport implements both interfaces; only an async-only
            # transport fails this isinstance.
            self._pool = transport_override
        else:
            raise TypeError(
                "SandboxClient needs a sync transport in transport_override"
            )
        self._timeout = self._options.timeout or DEFAULT_TIMEOUT
        # API-key auth, not the auth transport: minting must not need a token.
        self._mint_http_client = httpx.Client(
            transport=self._pool,
            base_url=_management_base_url(self._options),
            headers=_request_headers(
                self._options,
                f"Bearer {api_key}" if api_key != "" else None,
            ),
        )
        self._token_source = SyncTokenSource(
            api_key=api_key,
            token_provider=token_provider,
            mint=self._mint if api_key != "" else None,
        )
        self._auth_transport = _SyncAuthTransport(self._pool, self._token_source)
        self._http_client = httpx.Client(
            transport=self._auth_transport,
            base_url=_management_base_url(self._options),
            headers=_request_headers(self._options, None),
            timeout=self._timeout,
        )
        self._api = baseten.client.managementapi.ApiClient(self._http_client)
        self._sandbox_http_clients: list[httpx.Client] = []

    def _mint(self) -> tuple[str, datetime]:
        # The mint client carries the API key, so minting cannot recurse
        # through the auth transport.
        minted = baseten.client.managementapi.ApiClient(
            self._mint_http_client
        ).post_token(
            request=baseten.client.managementapi.CreateTokenRequest(
                scopes=[baseten.client.managementapi.TokenScope.sandboxes]
            )
        )
        return minted.token, minted.expires_at

    @property
    def options(self) -> SandboxClientOptions:
        """The options this client was constructed with."""
        return self._options

    @property
    def api(self) -> baseten.client.managementapi.ApiClient:
        """The generated management API client, authenticated for sandboxes.

        For operations this SDK does not wrap. The generated API surface is
        not covered by stability guarantees and may change between versions.
        """
        return self._api

    def create(
        self,
        *,
        name: str | None = None,
        image: str | None = None,
        memory: int | None = None,
        region: str | None = None,
        envs: dict[str, SandboxEnvValue] | None = None,
        labels: dict[str, str] | None = None,
        display_name: str | None = None,
        external_id: str | None = None,
    ) -> Sandbox:
        """Create a sandbox.

        Defaults for image and memory live server-side, so nothing is sent
        for what the caller leaves unset.
        """
        try:
            sandbox = self._api.create_sandbox(
                params=baseten.client.managementapi.CreateSandboxParams(
                    team_id=self._options.team_id
                ),
                request=_sandbox_create_request(
                    name=name,
                    image=image,
                    memory=memory,
                    region=region,
                    envs=sandbox_envs_to_api(envs) if envs is not None else None,
                    labels=labels,
                    display_name=display_name,
                    external_id=external_id,
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        info = sandbox_info_from_api(sandbox)
        return self._sandbox_for(info, info)

    def get_info(self, name: str) -> SandboxInfo:
        """Get a sandbox's current record."""
        try:
            sandbox = self._api.get_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.GetSandboxParams(
                    team_id=self._options.team_id
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        return sandbox_info_from_api(sandbox)

    def get(self, sandbox: str | SandboxInfo) -> Sandbox:
        """Get a :class:`Sandbox` to work in.

        Fetches the record when given a name; none is fetched when the
        record is already in hand.
        """
        info = self.get_info(sandbox) if isinstance(sandbox, str) else sandbox
        return self._sandbox_for(info)

    def list(
        self,
        *,
        query: str | None = None,
        statuses: Sequence[str] | None = None,
        external_id: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[SandboxInfo]:
        """List sandboxes, fetching further pages as iteration reaches them."""
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while True:
            try:
                response = self._api.list_sandboxes(
                    params=baseten.client.managementapi.ListSandboxesParams(
                        team_id=self._options.team_id,
                        cursor=cursor,
                        q=query,
                        status=[
                            baseten.client.managementapi.StatusItem(root=status)
                            for status in statuses
                        ]
                        if statuses is not None
                        else None,
                        external_id=external_id,
                        **({"limit": page_size} if page_size is not None else {}),
                    )
                )
            except Exception as error:
                raise to_sandbox_api_error(error, "control") from error
            yield from (sandbox_info_from_api(sb) for sb in response.items)
            cursor = (
                response.pagination.cursor if response.pagination.has_more else None
            )
            if cursor is None:
                return
            if cursor in seen_cursors:
                raise ValueError("sandbox list returned a repeated cursor")
            seen_cursors.add(cursor)

    def delete(self, name: str) -> SandboxInfo:
        """Delete a sandbox.

        Deletion continues after this returns, so the returned record usually
        shows the sandbox as still deleting.
        """
        try:
            sandbox = self._api.delete_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.DeleteSandboxParams(
                    team_id=self._options.team_id
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        return sandbox_info_from_api(sandbox)

    def close(self) -> None:
        """Close the client and the connections every sandbox shares."""
        for http_client in self._sandbox_http_clients:
            http_client.close()
        self._http_client.close()
        self._mint_http_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _sandbox_for(
        self, info: SandboxInfo, creation_info: SandboxInfo | None = None
    ) -> Sandbox:
        if info.url is None:
            raise ValueError(f"sandbox {info.name} has no URL yet")
        http_client = httpx.Client(
            transport=self._auth_transport,
            base_url=info.url,
            headers=_request_headers(self._options, None),
            timeout=self._timeout,
        )
        self._sandbox_http_clients.append(http_client)
        return Sandbox._shared(
            name=info.name,
            url=info.url,
            http_client=http_client,
            retries=self._options.retries,
            creation_info=creation_info,
        )


class AsyncSandboxClient:
    """Asynchronous client for Baseten sandboxes.

    Creates, finds, and deletes sandboxes, and hands out
    :class:`AsyncSandbox` objects to work in one. Closing the client closes
    the sandboxes' shared connections. Can be used as an async context
    manager.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token_provider: SandboxTokenProvider | AsyncSandboxTokenProvider | None = None,
        team_id: str | None = None,
        base_url_override: str | None = None,
        headers: Mapping[str, str] | None = None,
        http2: bool | None = None,
        timeout: httpx.Timeout | None = None,
        retries: SandboxRetryOptions | None = None,
        transport_override: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a new asynchronous sandbox client.

        The token provider may return a string or an awaitable of one. Every
        other argument matches the :class:`SandboxClientOptions` field of
        the same name.
        """
        self._options = SandboxClientOptions(
            api_key=api_key,
            token_provider=token_provider,
            team_id=team_id,
            base_url_override=base_url_override,
            headers=headers,
            http2=http2,
            timeout=timeout,
            retries=retries,
            transport_override=transport_override,
        )
        if transport_override is None:
            self._pool: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(
                http2=resolve_http2_flag(self._options.http2)
            )
        elif isinstance(transport_override, httpx.AsyncBaseTransport):
            self._pool = transport_override
        else:
            raise TypeError(
                "AsyncSandboxClient needs an async transport in transport_override"
            )
        self._timeout = self._options.timeout or DEFAULT_TIMEOUT
        self._mint_http_client = httpx.AsyncClient(
            transport=self._pool,
            base_url=_management_base_url(self._options),
            headers=_request_headers(
                self._options,
                f"Bearer {api_key}" if api_key != "" else None,
            ),
        )

        async def mint() -> tuple[str, datetime]:
            minted = await baseten.client.managementapi.AsyncApiClient(
                self._mint_http_client
            ).post_token(
                request=baseten.client.managementapi.CreateTokenRequest(
                    scopes=[baseten.client.managementapi.TokenScope.sandboxes]
                )
            )
            return minted.token, minted.expires_at

        self._token_source = AsyncTokenSource(
            api_key=api_key,
            token_provider=self._options.token_provider,  # type: ignore[arg-type]
            mint=mint if api_key != "" else None,
        )
        self._auth_transport = _AsyncAuthTransport(self._pool, self._token_source)
        self._http_client = httpx.AsyncClient(
            transport=self._auth_transport,
            base_url=_management_base_url(self._options),
            headers=_request_headers(self._options, None),
            timeout=self._timeout,
        )
        self._api = baseten.client.managementapi.AsyncApiClient(self._http_client)
        self._sandbox_http_clients: list[httpx.AsyncClient] = []

    @property
    def options(self) -> SandboxClientOptions:
        """The options this client was constructed with."""
        return self._options

    @property
    def api(self) -> baseten.client.managementapi.AsyncApiClient:
        """The generated management API client, authenticated for sandboxes.

        For operations this SDK does not wrap. The generated API surface is
        not covered by stability guarantees and may change between versions.
        """
        return self._api

    async def create(
        self,
        *,
        name: str | None = None,
        image: str | None = None,
        memory: int | None = None,
        region: str | None = None,
        envs: dict[str, SandboxEnvValue] | None = None,
        labels: dict[str, str] | None = None,
        display_name: str | None = None,
        external_id: str | None = None,
    ) -> AsyncSandbox:
        """Create a sandbox.

        Defaults for image and memory live server-side, so nothing is sent
        for what the caller leaves unset.
        """
        try:
            sandbox = await self._api.create_sandbox(
                params=baseten.client.managementapi.CreateSandboxParams(
                    team_id=self._options.team_id
                ),
                request=_sandbox_create_request(
                    name=name,
                    image=image,
                    memory=memory,
                    region=region,
                    envs=sandbox_envs_to_api(envs) if envs is not None else None,
                    labels=labels,
                    display_name=display_name,
                    external_id=external_id,
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        info = sandbox_info_from_api(sandbox)
        return await self._sandbox_for(info, info)

    async def get_info(self, name: str) -> SandboxInfo:
        """Get a sandbox's current record."""
        try:
            sandbox = await self._api.get_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.GetSandboxParams(
                    team_id=self._options.team_id
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        return sandbox_info_from_api(sandbox)

    async def get(self, sandbox: str | SandboxInfo) -> AsyncSandbox:
        """Get an :class:`AsyncSandbox` to work in.

        Fetches the record when given a name; none is fetched when the
        record is already in hand.
        """
        info = await self.get_info(sandbox) if isinstance(sandbox, str) else sandbox
        return await self._sandbox_for(info)

    async def list(
        self,
        *,
        query: str | None = None,
        statuses: Sequence[str] | None = None,
        external_id: str | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[SandboxInfo]:
        """List sandboxes, fetching further pages as iteration reaches them."""
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while True:
            try:
                response = await self._api.list_sandboxes(
                    params=baseten.client.managementapi.ListSandboxesParams(
                        team_id=self._options.team_id,
                        cursor=cursor,
                        q=query,
                        status=[
                            baseten.client.managementapi.StatusItem(root=status)
                            for status in statuses
                        ]
                        if statuses is not None
                        else None,
                        external_id=external_id,
                        **({"limit": page_size} if page_size is not None else {}),
                    )
                )
            except Exception as error:
                raise to_sandbox_api_error(error, "control") from error
            for sb in response.items:
                yield sandbox_info_from_api(sb)
            cursor = (
                response.pagination.cursor if response.pagination.has_more else None
            )
            if cursor is None:
                return
            if cursor in seen_cursors:
                raise ValueError("sandbox list returned a repeated cursor")
            seen_cursors.add(cursor)

    async def delete(self, name: str) -> SandboxInfo:
        """Delete a sandbox.

        Deletion continues after this returns, so the returned record usually
        shows the sandbox as still deleting.
        """
        try:
            sandbox = await self._api.delete_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.DeleteSandboxParams(
                    team_id=self._options.team_id
                ),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "control") from error
        return sandbox_info_from_api(sandbox)

    async def close(self) -> None:
        """Close the client and the connections every sandbox shares."""
        for http_client in self._sandbox_http_clients:
            await http_client.aclose()
        await self._http_client.aclose()
        await self._mint_http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _sandbox_for(
        self, info: SandboxInfo, creation_info: SandboxInfo | None = None
    ) -> AsyncSandbox:
        if info.url is None:
            raise ValueError(f"sandbox {info.name} has no URL yet")
        http_client = httpx.AsyncClient(
            transport=self._auth_transport,
            base_url=info.url,
            headers=_request_headers(self._options, None),
            timeout=self._timeout,
        )
        self._sandbox_http_clients.append(http_client)
        return AsyncSandbox._shared(
            name=info.name,
            url=info.url,
            http_client=http_client,
            retries=self._options.retries,
            creation_info=creation_info,
        )

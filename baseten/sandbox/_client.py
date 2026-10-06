from __future__ import annotations

import importlib.util
from collections.abc import (
    AsyncIterator,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Self, overload

import httpx

import baseten.client
import baseten.client.managementapi
import baseten.client.sandboxapi
from baseten.client._user_agent import with_user_agent
from baseten.sandbox._auth import (
    TOKEN_MINT_TIMEOUT,
    AsyncSandboxTokenProvider,
    AsyncTokenAuth,
    AsyncTokenSource,
    SandboxTokenProvider,
    TokenAuth,
    TokenSource,
)
from baseten.sandbox._common import (
    AsyncSandboxContext,
    SandboxContext,
    paginate,
    paginate_async,
)
from baseten.sandbox._errors import converted_errors
from baseten.sandbox._images import (
    AsyncImageClient,
    AsyncImageClientContext,
    ImageClient,
    ImageClientContext,
)
from baseten.sandbox._info import (
    SandboxEnvValue,
    SandboxInfo,
    SandboxLifecycle,
    SandboxNetwork,
    SandboxPort,
    SandboxStatus,
    envs_to_api,
    lifecycle_to_api,
    network_to_api,
    ports_to_api,
    sandbox_info_from_api,
    set_fields,
)
from baseten.sandbox._retry import SandboxRetryOptions, resolve_retry_options
from baseten.sandbox._sandbox import AsyncSandbox, Sandbox

# Timeouts of the HTTP client a sandbox client creates. No read timeout: a
# process run that waits for completion sends response headers only when the
# process exits. Connecting gets longer than httpx's 5s default, while writes
# and pool waits keep it.
_DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=10.0, read=None)


@dataclass(frozen=True)
class SandboxClientOptions:
    """Options for :class:`SandboxClient` and :class:`AsyncSandboxClient`.

    Obtain via :attr:`SandboxClient.options` to inspect the values a client
    was constructed with.
    """

    api_key: str
    """API key for authentication."""

    team_id: str | None = None
    """Team to act in, or ``None`` for the caller's only team."""

    token_provider: SandboxTokenProvider | AsyncSandboxTokenProvider | None = None
    """Provider of each request's bearer token, in place of the API key."""

    base_url_override: str | None = None
    """Explicit base URL override, or ``None`` to use the default."""

    headers: Mapping[str, str] | None = None
    """Additional headers to send on every request."""

    retry: SandboxRetryOptions | None = None
    """Retry budgets for transient failures, or ``None`` for the defaults."""

    http2: bool | None = None
    """Whether the client's own HTTP client uses HTTP/2."""

    @property
    def base_url(self) -> str:
        """The resolved base URL for the management API."""
        if self.base_url_override is not None:
            return self.base_url_override
        return baseten.client.ManagementClient.default_base_url()


class SandboxCreateResult(Sandbox):
    """A newly created sandbox, with its record as of creation."""

    info: SandboxInfo
    """The sandbox as reported when creation returned."""

    def __init__(self, context: SandboxContext, info: SandboxInfo) -> None:
        """Internal. Returned by :meth:`SandboxClient.create`.

        :meta private:
        """
        super().__init__(context)
        self.info = info


class AsyncSandboxCreateResult(AsyncSandbox):
    """Async form of :class:`SandboxCreateResult`."""

    info: SandboxInfo
    """The sandbox as reported when creation returned."""

    def __init__(self, context: AsyncSandboxContext, info: SandboxInfo) -> None:
        """Internal. Returned by :meth:`AsyncSandboxClient.create`.

        :meta private:
        """
        super().__init__(context)
        self.info = info


class SandboxClient:
    """Client for Baseten sandboxes.

    Creates, finds, and deletes sandboxes, and gets a :class:`Sandbox` to work
    in one. Can be used as a context manager to ensure the underlying HTTP
    client is closed on exit.
    """

    def __init__(
        self,
        *,
        api_key: str,
        team_id: str | None = None,
        token_provider: SandboxTokenProvider | None = None,
        base_url_override: str | None = None,
        headers: Mapping[str, str] | None = None,
        retry: SandboxRetryOptions | None = None,
        http2: bool | None = None,
        http_client_override: httpx.Client | None = None,
        close_http_client_on_close: bool | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        """Create a new sandbox client.

        Args:
            api_key: API key for authentication. Empty, together with
                *token_provider*, is the advanced opt-out of API key
                authentication.
            team_id: Team to act in. ``None`` uses the caller's only team,
                and is an error for callers in more than one team.
            token_provider: Returns the bearer token for each request, instead
                of one derived from the API key, which must then be empty.
            base_url_override: Override the management API's base URL, which
                defaults to ``https://api.baseten.co``.
            headers: Additional headers to send on every request.
            retry: Retry budgets for transient failures. ``None`` uses the
                defaults.
            http2: Whether the HTTP client this client creates uses HTTP/2,
                which matters for performance. ``None`` uses it when the
                ``h2`` package is installed, as with ``httpx[http2]``.
                ``True`` requires it, and ``False`` never uses it. Has no
                effect with *http_client_override*.
            http_client_override: Pre-configured httpx client to send every
                request through, such as another sandbox client's
                :attr:`http_client` to share its connections. Requests carry
                absolute URLs and their own authentication and headers, so it
                needs none of its own. Its timeouts apply, and a read timeout
                cuts off long process runs and streams.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
            executor: Thread pool for uploading a large file's parts in
                parallel, on top of the calling thread. Never shut down by
                this client. ``None`` gives each :class:`Sandbox` its own pool
                of 2 threads, created on its first large upload, whose idle
                threads end once the sandbox is no longer referenced.
        """
        self._executor = executor
        self._options = SandboxClientOptions(
            api_key=api_key,
            team_id=team_id,
            token_provider=token_provider,
            base_url_override=base_url_override,
            headers=headers,
            retry=retry,
            http2=http2,
        )
        if http_client_override is None:
            self._http_client = httpx.Client(
                http2=_use_http2(http2), timeout=_DEFAULT_TIMEOUT
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
        self._request_headers = with_user_agent(headers or {})
        mint_api = baseten.client.managementapi.ApiClient(
            self._http_client,
            base_url=self._options.base_url,
            headers={**self._request_headers, "Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(TOKEN_MINT_TIMEOUT),
        )
        self._auth = TokenAuth(
            TokenSource(
                api_key=api_key, token_provider=token_provider, mint_api=mint_api
            )
        )
        self._raw_api = baseten.client.managementapi.ApiClient(
            self._http_client,
            base_url=self._options.base_url,
            auth=self._auth,
            headers=self._request_headers,
        )
        self._retry = resolve_retry_options(retry)
        self._images = ImageClient(
            ImageClientContext(
                raw_api=self._raw_api,
                team_id=team_id,
                http_client=self._http_client,
            )
        )

    @property
    def options(self) -> SandboxClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.Client:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def images(self) -> ImageClient:
        """Images that sandboxes are created from."""
        return self._images

    @property
    def raw_api(self) -> baseten.client.managementapi.ApiClient:
        """The generated management API client, authenticated for sandboxes.

        For operations this SDK does not wrap. The generated API surface is not
        covered by stability guarantees and may change between versions.
        """
        return self._raw_api

    def create(
        self,
        *,
        name: str | None = None,
        create_if_not_exists: bool | None = None,
        image: str | None = None,
        memory: int | None = None,
        region: str | None = None,
        envs: Mapping[str, SandboxEnvValue] | None = None,
        labels: Mapping[str, str] | None = None,
        external_id: str | None = None,
        lifecycle: SandboxLifecycle | None = None,
        ports: Sequence[SandboxPort] | None = None,
        network: SandboxNetwork | None = None,
    ) -> SandboxCreateResult:
        """Create a sandbox.

        Args:
            name: Unique name of the sandbox. Assigned by the server when
                ``None``.
            create_if_not_exists: When true, return the existing live sandbox
                with this name, or recreate it if it is failed, terminated, or
                being deleted. Requires *name*. Existing configuration is
                preserved.
            image: Image reference including its tag. Defaults to
                ``baseten/base-image:latest``.
            memory: Memory in megabytes, which also sets the CPU allocation.
                Defaults to 4096.
            region: Region to run in. ``None`` picks the closest one.
            envs: Environment variables, by name.
            labels: Labels, by key.
            external_id: Caller-owned identifier for external lookups.
            lifecycle: When the sandbox is deleted automatically.
            ports: Ports the sandbox exposes.
            network: Network configuration, which cannot be changed after
                creation.
        """
        with self._control_api() as api:
            sandbox = api.create_sandbox(
                params=baseten.client.managementapi.CreateSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxRequest(
                    **set_fields(
                        name=name,
                        create_if_not_exists=create_if_not_exists,
                        image=image,
                        memory=memory,
                        region=region,
                        envs=None if envs is None else envs_to_api(envs),
                        labels=None if labels is None else dict(labels),
                        external_id=external_id,
                        lifecycle=None
                        if lifecycle is None
                        else lifecycle_to_api(lifecycle),
                        ports=None if ports is None else ports_to_api(ports),
                        network=None if network is None else network_to_api(network),
                    )
                ),
            )
        info = sandbox_info_from_api(sandbox)
        return SandboxCreateResult(self._sandbox_context(info.name, info.url), info)

    def get_info(self, *, name: str, show_secrets: bool | None = None) -> SandboxInfo:
        """Get a sandbox's current record.

        Args:
            name: Name of the sandbox.
            show_secrets: Reveal environment variable values for workspace
                administrators. Defaults to false. Callers without the admin
                role receive masked values even when true.
        """
        with self._control_api() as api:
            sandbox = api.get_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.GetSandboxParams(
                    **set_fields(
                        team_id=self._options.team_id, show_secrets=show_secrets
                    )
                ),
            )
        return sandbox_info_from_api(sandbox)

    @overload
    def get(self, *, name: str) -> Sandbox: ...

    @overload
    def get(self, *, info: SandboxInfo) -> Sandbox: ...

    def get(
        self, *, name: str | None = None, info: SandboxInfo | None = None
    ) -> Sandbox:
        """Get a :class:`Sandbox` to work in.

        Fetches the sandbox's record when given a name, and makes no call when
        given the record.

        Args:
            name: Name of the sandbox.
            info: The sandbox's record, already in hand.
        """
        if info is None:
            if name is None:
                raise TypeError("exactly one of name or info must be given")
            info = self.get_info(name=name)
        elif name is not None:
            raise TypeError("exactly one of name or info must be given")
        return Sandbox(self._sandbox_context(info.name, info.url))

    def sandbox_from_url(self, *, url: str) -> Sandbox:
        """Get a :class:`Sandbox` for a sandbox's execution API URL, making no call.

        Its :attr:`Sandbox.name` is empty.

        Args:
            url: Base URL of the sandbox's execution API, as reported in
                :attr:`SandboxInfo.url`.
        """
        return Sandbox(self._sandbox_context("", url))

    def list(
        self,
        *,
        query: str | None = None,
        statuses: Sequence[SandboxStatus] | None = None,
        external_id: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[SandboxInfo]:
        """List sandboxes, fetching further pages as iteration reaches them.

        Args:
            query: Searches sandbox names and labels.
            statuses: Only sandboxes with one of these statuses. Cannot be
                combined with *external_id*.
            external_id: Only the sandbox with this external identifier.
                Cannot be combined with *statuses*.
            page_size: How many sandboxes to fetch per underlying request.
        """

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxesResponse:
            with self._control_api() as api:
                return api.list_sandboxes(
                    params=baseten.client.managementapi.ListSandboxesParams(
                        **set_fields(
                            team_id=self._options.team_id,
                            cursor=cursor,
                            limit=page_size,
                            q=query,
                            status=None if statuses is None else [*statuses],
                            external_id=external_id,
                        )
                    )
                )

        for sandbox in paginate("sandbox list", fetch_page):
            yield sandbox_info_from_api(sandbox)

    def update(
        self,
        *,
        name: str,
        lifecycle: SandboxLifecycle | None = None,
        envs: Mapping[str, SandboxEnvValue] | None = None,
        external_id: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> SandboxInfo:
        """Update a sandbox's configuration, returning its record after the change.

        Arguments left ``None`` stay unchanged, and set ones replace their
        previous values, including every entry of *envs*, *labels*, and the
        lifecycle's expiration policies. Other lifecycle fields change only
        when set. At least one argument besides *name* must be set. Other
        settings cannot be changed after creation.

        Args:
            name: Name of the sandbox.
            lifecycle: When the sandbox is deleted automatically.
            envs: Environment variables, by name.
            external_id: Caller-owned identifier for external lookups.
            labels: Labels, by key.
        """
        with self._control_api() as api:
            sandbox = api.update_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.UpdateSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
                request=baseten.client.managementapi.UpdateSandboxRequest(
                    **set_fields(
                        lifecycle=None
                        if lifecycle is None
                        else lifecycle_to_api(lifecycle),
                        envs=None if envs is None else envs_to_api(envs),
                        external_id=external_id,
                        labels=None if labels is None else dict(labels),
                    )
                ),
            )
        return sandbox_info_from_api(sandbox)

    def delete(self, *, name: str) -> SandboxInfo:
        """Delete a sandbox.

        Deletion continues after this returns, so the returned record usually
        shows it as still deleting.

        Args:
            name: Name of the sandbox.
        """
        with self._control_api() as api:
            sandbox = api.delete_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.DeleteSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
            )
        return sandbox_info_from_api(sandbox)

    def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            self._http_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def _control_api(self) -> Iterator[baseten.client.managementapi.ApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("control"):
            yield self._raw_api

    def _sandbox_context(self, name: str, url: str) -> SandboxContext:
        """Build what a :class:`Sandbox` for the given sandbox sends with."""
        return SandboxContext(
            name=name,
            url=url,
            http_client=self._http_client,
            auth=self._auth,
            headers=self._request_headers,
            raw_api=baseten.client.sandboxapi.ApiClient(
                self._http_client,
                base_url=url,
                auth=self._auth,
                headers=self._request_headers,
            ),
            retry=self._retry,
            executor=self._executor,
        )


class AsyncSandboxClient:
    """Async form of :class:`SandboxClient`.

    Can be used as an async context manager to ensure the underlying HTTP
    client is closed on exit.
    """

    def __init__(
        self,
        *,
        api_key: str,
        team_id: str | None = None,
        token_provider: AsyncSandboxTokenProvider | None = None,
        base_url_override: str | None = None,
        headers: Mapping[str, str] | None = None,
        retry: SandboxRetryOptions | None = None,
        http2: bool | None = None,
        http_client_override: httpx.AsyncClient | None = None,
        close_http_client_on_close: bool | None = None,
    ) -> None:
        """Create a new async sandbox client.

        Args:
            api_key: API key for authentication. Empty, together with
                *token_provider*, is the advanced opt-out of API key
                authentication.
            team_id: Team to act in. ``None`` uses the caller's only team,
                and is an error for callers in more than one team.
            token_provider: Returns the bearer token for each request, instead
                of one derived from the API key, which must then be empty.
            base_url_override: Override the management API's base URL, which
                defaults to ``https://api.baseten.co``.
            headers: Additional headers to send on every request.
            retry: Retry budgets for transient failures. ``None`` uses the
                defaults.
            http2: Whether the HTTP client this client creates uses HTTP/2,
                which matters for performance. ``None`` uses it when the
                ``h2`` package is installed, as with ``httpx[http2]``.
                ``True`` requires it, and ``False`` never uses it. Has no
                effect with *http_client_override*.
            http_client_override: Pre-configured httpx async client to send
                every request through, such as another sandbox client's
                :attr:`http_client` to share its connections. Requests carry
                absolute URLs and their own authentication and headers, so it
                needs none of its own. Its timeouts apply, and a read timeout
                cuts off long process runs and streams.
            close_http_client_on_close: Whether :meth:`close` should close
                the underlying HTTP client. Defaults to ``True`` when the
                client is created internally, ``False`` when
                *http_client_override* is provided.
        """
        self._options = SandboxClientOptions(
            api_key=api_key,
            team_id=team_id,
            token_provider=token_provider,
            base_url_override=base_url_override,
            headers=headers,
            retry=retry,
            http2=http2,
        )
        if http_client_override is None:
            self._http_client = httpx.AsyncClient(
                http2=_use_http2(http2), timeout=_DEFAULT_TIMEOUT
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
        self._request_headers = with_user_agent(headers or {})
        mint_api = baseten.client.managementapi.AsyncApiClient(
            self._http_client,
            base_url=self._options.base_url,
            headers={**self._request_headers, "Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(TOKEN_MINT_TIMEOUT),
        )
        self._auth = AsyncTokenAuth(
            AsyncTokenSource(
                api_key=api_key, token_provider=token_provider, mint_api=mint_api
            )
        )
        self._raw_api = baseten.client.managementapi.AsyncApiClient(
            self._http_client,
            base_url=self._options.base_url,
            auth=self._auth,
            headers=self._request_headers,
        )
        self._retry = resolve_retry_options(retry)
        self._images = AsyncImageClient(
            AsyncImageClientContext(
                raw_api=self._raw_api,
                team_id=team_id,
                http_client=self._http_client,
            )
        )

    @property
    def options(self) -> SandboxClientOptions:
        """Client options."""
        return self._options

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._http_client

    @property
    def images(self) -> AsyncImageClient:
        """Images that sandboxes are created from."""
        return self._images

    @property
    def raw_api(self) -> baseten.client.managementapi.AsyncApiClient:
        """The generated management API client, authenticated for sandboxes.

        For operations this SDK does not wrap. The generated API surface is not
        covered by stability guarantees and may change between versions.
        """
        return self._raw_api

    async def create(
        self,
        *,
        name: str | None = None,
        create_if_not_exists: bool | None = None,
        image: str | None = None,
        memory: int | None = None,
        region: str | None = None,
        envs: Mapping[str, SandboxEnvValue] | None = None,
        labels: Mapping[str, str] | None = None,
        external_id: str | None = None,
        lifecycle: SandboxLifecycle | None = None,
        ports: Sequence[SandboxPort] | None = None,
        network: SandboxNetwork | None = None,
    ) -> AsyncSandboxCreateResult:
        """Create a sandbox. As :meth:`SandboxClient.create`."""
        with self._control_api() as api:
            sandbox = await api.create_sandbox(
                params=baseten.client.managementapi.CreateSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxRequest(
                    **set_fields(
                        name=name,
                        create_if_not_exists=create_if_not_exists,
                        image=image,
                        memory=memory,
                        region=region,
                        envs=None if envs is None else envs_to_api(envs),
                        labels=None if labels is None else dict(labels),
                        external_id=external_id,
                        lifecycle=None
                        if lifecycle is None
                        else lifecycle_to_api(lifecycle),
                        ports=None if ports is None else ports_to_api(ports),
                        network=None if network is None else network_to_api(network),
                    )
                ),
            )
        info = sandbox_info_from_api(sandbox)
        return AsyncSandboxCreateResult(
            self._sandbox_context(info.name, info.url), info
        )

    async def get_info(
        self, *, name: str, show_secrets: bool | None = None
    ) -> SandboxInfo:
        """Get a sandbox's current record. As :meth:`SandboxClient.get_info`."""
        with self._control_api() as api:
            sandbox = await api.get_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.GetSandboxParams(
                    **set_fields(
                        team_id=self._options.team_id, show_secrets=show_secrets
                    )
                ),
            )
        return sandbox_info_from_api(sandbox)

    @overload
    async def get(self, *, name: str) -> AsyncSandbox: ...

    @overload
    async def get(self, *, info: SandboxInfo) -> AsyncSandbox: ...

    async def get(
        self, *, name: str | None = None, info: SandboxInfo | None = None
    ) -> AsyncSandbox:
        """Get an :class:`AsyncSandbox` to work in. As :meth:`SandboxClient.get`."""
        if info is None:
            if name is None:
                raise TypeError("exactly one of name or info must be given")
            info = await self.get_info(name=name)
        elif name is not None:
            raise TypeError("exactly one of name or info must be given")
        return AsyncSandbox(self._sandbox_context(info.name, info.url))

    def sandbox_from_url(self, *, url: str) -> AsyncSandbox:
        """Get an :class:`AsyncSandbox` for a URL. As :meth:`SandboxClient.sandbox_from_url`."""
        return AsyncSandbox(self._sandbox_context("", url))

    async def list(
        self,
        *,
        query: str | None = None,
        statuses: Sequence[SandboxStatus] | None = None,
        external_id: str | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[SandboxInfo]:
        """List sandboxes. As :meth:`SandboxClient.list`."""

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxesResponse:
            with self._control_api() as api:
                return await api.list_sandboxes(
                    params=baseten.client.managementapi.ListSandboxesParams(
                        **set_fields(
                            team_id=self._options.team_id,
                            cursor=cursor,
                            limit=page_size,
                            q=query,
                            status=None if statuses is None else [*statuses],
                            external_id=external_id,
                        )
                    )
                )

        async for sandbox in paginate_async("sandbox list", fetch_page):
            yield sandbox_info_from_api(sandbox)

    async def update(
        self,
        *,
        name: str,
        lifecycle: SandboxLifecycle | None = None,
        envs: Mapping[str, SandboxEnvValue] | None = None,
        external_id: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> SandboxInfo:
        """Update a sandbox's configuration. As :meth:`SandboxClient.update`."""
        with self._control_api() as api:
            sandbox = await api.update_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.UpdateSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
                request=baseten.client.managementapi.UpdateSandboxRequest(
                    **set_fields(
                        lifecycle=None
                        if lifecycle is None
                        else lifecycle_to_api(lifecycle),
                        envs=None if envs is None else envs_to_api(envs),
                        external_id=external_id,
                        labels=None if labels is None else dict(labels),
                    )
                ),
            )
        return sandbox_info_from_api(sandbox)

    async def delete(self, *, name: str) -> SandboxInfo:
        """Delete a sandbox. As :meth:`SandboxClient.delete`."""
        with self._control_api() as api:
            sandbox = await api.delete_sandbox(
                sandbox_name=name,
                params=baseten.client.managementapi.DeleteSandboxParams(
                    **set_fields(team_id=self._options.team_id)
                ),
            )
        return sandbox_info_from_api(sandbox)

    async def close(self) -> None:
        """Close the client, optionally closing the underlying HTTP client."""
        if self.close_http_client_on_close:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    @contextmanager
    def _control_api(self) -> Iterator[baseten.client.managementapi.AsyncApiClient]:
        """Give the generated client, raising its error responses as ours."""
        # A plain context manager suffices, since it only wraps awaited calls.
        with converted_errors("control"):
            yield self._raw_api

    def _sandbox_context(self, name: str, url: str) -> AsyncSandboxContext:
        """Build what an :class:`AsyncSandbox` for the given sandbox sends with."""
        return AsyncSandboxContext(
            name=name,
            url=url,
            http_client=self._http_client,
            auth=self._auth,
            headers=self._request_headers,
            raw_api=baseten.client.sandboxapi.AsyncApiClient(
                self._http_client,
                base_url=url,
                auth=self._auth,
                headers=self._request_headers,
            ),
            retry=self._retry,
        )


def _use_http2(http2: bool | None) -> bool:
    """Resolve the ``http2`` option against whether ``h2`` is installed."""
    if http2 is False:
        return False
    available = importlib.util.find_spec("h2") is not None
    if http2 and not available:
        raise ImportError(
            'http2=True requires the h2 package, installed with "httpx[http2]"'
        )
    return available

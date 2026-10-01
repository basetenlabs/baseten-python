from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Self

import httpx

import baseten.client.sandboxapi
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
    SandboxInfo,
    SandboxProcessInfo,
    SandboxProcessLogs,
    process_info_from_api,
    process_logs_from_api,
)
from baseten.sandbox._retry import (
    SandboxRetryOptions,
    retry_idempotent,
    retry_idempotent_async,
)

_NOT_IMPLEMENTED = (
    "Not implemented yet. Reach the operation through {} in the meantime."
)


class Sandbox:
    """One sandbox, reached directly at its own URL.

    Get one from :meth:`baseten.sandbox.SandboxClient.create` or
    :meth:`baseten.sandbox.SandboxClient.get`, or construct one directly when
    the URL and a token provider are already known. A sandbox from a client
    shares that client's connections and is closed with it; one constructed
    directly owns its connections and :meth:`close` closes them.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        token_provider: SandboxTokenProvider | None = None,
        headers: Mapping[str, str] | None = None,
        http2: bool | None = None,
        timeout: httpx.Timeout | None = None,
        retries: SandboxRetryOptions | None = None,
        transport_override: httpx.BaseTransport | None = None,
    ) -> None:
        """Construct a standalone sandbox client.

        Args:
            name: Name of the sandbox.
            url: Base URL of the sandbox's execution API, as reported in
                ``SandboxInfo.url``.
            token_provider: Returns the bearer token for each request. When
                unset, no Authorization is sent.
            headers: Additional headers to send on every request.
            http2: Use HTTP/2 when the optional h2 package allows. None
                enables it when h2 is importable, True requires it.
            timeout: HTTP timeouts. Defaults to bounding only connect time.
            retries: Retry budgets for transient failures.
            transport_override: HTTP transport for tests with a mock.
        """
        self._name = name
        self._url = url.rstrip("/")
        self._retries = retries or SandboxRetryOptions()
        self._pool = (
            transport_override
            if transport_override is not None
            else httpx.HTTPTransport(http2=resolve_http2_flag(http2))
        )
        self._token_source = SyncTokenSource(
            api_key="", token_provider=token_provider, mint=None
        )
        self._auth_transport = _SyncAuthTransport(self._pool, self._token_source)
        request_headers = with_user_agent({**(headers or {})})
        self._http_client = httpx.Client(
            transport=self._auth_transport,
            base_url=self._url,
            headers=request_headers,
            timeout=timeout
            if timeout is not None
            else httpx.Timeout(None, connect=10.0),
        )
        self._api = baseten.client.sandboxapi.ApiClient(self._http_client)
        self._fs: SandboxFileSystem | None = None
        self._process: SandboxProcess | None = None
        self._info: SandboxInfo | None = None
        self._owns_pool = True

    @classmethod
    def _shared(
        cls,
        *,
        name: str,
        url: str,
        http_client: httpx.Client,
        retries: SandboxRetryOptions | None,
        creation_info: SandboxInfo | None,
    ) -> Sandbox:
        sandbox = cls.__new__(cls)
        sandbox._name = name
        sandbox._url = url.rstrip("/")
        sandbox._retries = retries or SandboxRetryOptions()
        sandbox._http_client = http_client
        sandbox._api = baseten.client.sandboxapi.ApiClient(http_client)
        sandbox._fs = None
        sandbox._process = None
        sandbox._info = creation_info
        sandbox._owns_pool = False
        return sandbox

    @property
    def name(self) -> str:
        """Name of the sandbox."""
        return self._name

    @property
    def url(self) -> str:
        """Base URL of the sandbox's execution API."""
        return self._url

    @property
    def info(self) -> SandboxInfo | None:
        """The record as of creation, when this sandbox came from
        :meth:`baseten.sandbox.SandboxClient.create`. None otherwise."""
        return self._info

    @property
    def api(self) -> baseten.client.sandboxapi.ApiClient:
        """The generated client for this sandbox's execution API.

        For operations this SDK does not wrap. The generated API surface is
        not covered by stability guarantees and may change between versions.
        """
        return self._api

    @property
    def fs(self) -> SandboxFileSystem:
        """Files and directories in the sandbox."""
        if self._fs is None:
            self._fs = SandboxFileSystem(self._api, self._retries)
        return self._fs

    @property
    def process(self) -> SandboxProcess:
        """Processes in the sandbox."""
        if self._process is None:
            self._process = SandboxProcess(self._api)
        return self._process

    @property
    def drives(self) -> object:
        """Drive mounts in the sandbox. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def codegen(self) -> object:
        """Code editing helpers. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def system(self) -> object:
        """Sandbox system operations. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def network(self) -> object:
        """Network access to ports in the sandbox. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    def close(self) -> None:
        """Close this sandbox's connections, when it owns them.

        A sandbox from a :class:`baseten.sandbox.SandboxClient` shares that
        client's connections and is closed with it, so this does nothing.
        """
        if self._owns_pool:
            self._http_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncSandbox:
    """One sandbox, reached directly at its own URL. Async variant.

    Get one from :meth:`baseten.sandbox.AsyncSandboxClient.create` or
    :meth:`baseten.sandbox.AsyncSandboxClient.get`, or construct one directly
    when the URL and a token provider are already known.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        token_provider: SandboxTokenProvider | AsyncSandboxTokenProvider | None = None,
        headers: Mapping[str, str] | None = None,
        http2: bool | None = None,
        timeout: httpx.Timeout | None = None,
        retries: SandboxRetryOptions | None = None,
        transport_override: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct a standalone async sandbox client.

        The token provider may return a string or an awaitable of one. Other
        args match :class:`Sandbox`.
        """
        self._name = name
        self._url = url.rstrip("/")
        self._retries = retries or SandboxRetryOptions()
        self._pool = (
            transport_override
            if transport_override is not None
            else httpx.AsyncHTTPTransport(http2=resolve_http2_flag(http2))
        )
        self._token_source = AsyncTokenSource(
            api_key="",
            token_provider=token_provider,
            mint=None,  # type: ignore[arg-type]
        )
        self._auth_transport = _AsyncAuthTransport(self._pool, self._token_source)
        request_headers = with_user_agent({**(headers or {})})
        self._http_client = httpx.AsyncClient(
            transport=self._auth_transport,
            base_url=self._url,
            headers=request_headers,
            timeout=timeout
            if timeout is not None
            else httpx.Timeout(None, connect=10.0),
        )
        self._api = baseten.client.sandboxapi.AsyncApiClient(self._http_client)
        self._fs: AsyncSandboxFileSystem | None = None
        self._process: AsyncSandboxProcess | None = None
        self._info: SandboxInfo | None = None
        self._owns_pool = True

    @classmethod
    def _shared(
        cls,
        *,
        name: str,
        url: str,
        http_client: httpx.AsyncClient,
        retries: SandboxRetryOptions | None,
        creation_info: SandboxInfo | None,
    ) -> AsyncSandbox:
        sandbox = cls.__new__(cls)
        sandbox._name = name
        sandbox._url = url.rstrip("/")
        sandbox._retries = retries or SandboxRetryOptions()
        sandbox._http_client = http_client
        sandbox._api = baseten.client.sandboxapi.AsyncApiClient(http_client)
        sandbox._fs = None
        sandbox._process = None
        sandbox._info = creation_info
        sandbox._owns_pool = False
        return sandbox

    @property
    def name(self) -> str:
        """Name of the sandbox."""
        return self._name

    @property
    def url(self) -> str:
        """Base URL of the sandbox's execution API."""
        return self._url

    @property
    def info(self) -> SandboxInfo | None:
        """The record as of creation, when this sandbox came from
        :meth:`baseten.sandbox.AsyncSandboxClient.create`. None otherwise."""
        return self._info

    @property
    def api(self) -> baseten.client.sandboxapi.AsyncApiClient:
        """The generated client for this sandbox's execution API.

        For operations this SDK does not wrap. The generated API surface is
        not covered by stability guarantees and may change between versions.
        """
        return self._api

    @property
    def fs(self) -> AsyncSandboxFileSystem:
        """Files and directories in the sandbox."""
        if self._fs is None:
            self._fs = AsyncSandboxFileSystem(self._api, self._retries)
        return self._fs

    @property
    def process(self) -> AsyncSandboxProcess:
        """Processes in the sandbox."""
        if self._process is None:
            self._process = AsyncSandboxProcess(self._api)
        return self._process

    @property
    def drives(self) -> object:
        """Drive mounts in the sandbox. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def codegen(self) -> object:
        """Code editing helpers. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def system(self) -> object:
        """Sandbox system operations. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    @property
    def network(self) -> object:
        """Network access to ports in the sandbox. Not implemented yet."""
        raise NotImplementedError(_NOT_IMPLEMENTED.format("sandbox.api"))

    async def close(self) -> None:
        """Close this sandbox's connections, when it owns them."""
        if self._owns_pool:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class SandboxFileSystem:
    """Files and directories in a sandbox."""

    def __init__(
        self, api: baseten.client.sandboxapi.ApiClient, retries: SandboxRetryOptions
    ) -> None:
        self._api = api
        self._retries = retries

    def read(self, path: str) -> str:
        """Read a text file.

        Retries dropped connections and edge gateway errors, since a read is
        safe to repeat.
        """

        def attempt() -> baseten.client.sandboxapi.GetFilesystemResponse:
            # Convert per attempt: the retry helper must see
            # SandboxGatewayError, not the generated ResponseError.
            try:
                return self._api.get_filesystem(path=path)
            except Exception as error:
                raise to_sandbox_api_error(error, "exec") from error

        result = retry_idempotent(
            attempt,
            max_retries=self._retries.read_max_retries,
            gateway_max_retries=self._retries.gateway_max_retries,
        )
        root = result.root
        if isinstance(root, baseten.client.sandboxapi.FileWithContent):
            return root.content
        if isinstance(root, bytes):
            return root.decode()
        raise ValueError(f"{path} is a directory, not a file")

    def write(self, path: str, content: str) -> None:
        """Write a text file, creating it or replacing its content."""
        try:
            self._api.put_filesystem(
                path=path,
                request=baseten.client.sandboxapi.FileRequest(content=content),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error


class AsyncSandboxFileSystem:
    """Files and directories in a sandbox. Async variant."""

    def __init__(
        self,
        api: baseten.client.sandboxapi.AsyncApiClient,
        retries: SandboxRetryOptions,
    ) -> None:
        self._api = api
        self._retries = retries

    async def read(self, path: str) -> str:
        """Read a text file.

        Retries dropped connections and edge gateway errors, since a read is
        safe to repeat.
        """

        async def attempt() -> baseten.client.sandboxapi.GetFilesystemResponse:
            # Convert per attempt: the retry helper must see
            # SandboxGatewayError, not the generated ResponseError.
            try:
                return await self._api.get_filesystem(path=path)
            except Exception as error:
                raise to_sandbox_api_error(error, "exec") from error

        result = await retry_idempotent_async(
            attempt,
            max_retries=self._retries.read_max_retries,
            gateway_max_retries=self._retries.gateway_max_retries,
        )
        root = result.root
        if isinstance(root, baseten.client.sandboxapi.FileWithContent):
            return root.content
        if isinstance(root, bytes):
            return root.decode()
        raise ValueError(f"{path} is a directory, not a file")

    async def write(self, path: str, content: str) -> None:
        """Write a text file, creating it or replacing its content."""
        try:
            await self._api.put_filesystem(
                path=path,
                request=baseten.client.sandboxapi.FileRequest(content=content),
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error


class SandboxProcess:
    """Processes in a sandbox."""

    def __init__(self, api: baseten.client.sandboxapi.ApiClient) -> None:
        self._api = api

    def exec(
        self,
        command: str,
        *,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        name: str | None = None,
        timeout_seconds: int | None = None,
        wait_for_completion: bool | None = None,
    ) -> SandboxProcessInfo:
        """Start a command.

        Never retried, since a command may have side effects even when its
        response is lost.
        """
        try:
            response = self._api.post_process(
                request=baseten.client.sandboxapi.ProcessRequest(
                    command=command,
                    workingDir=working_dir,
                    env=env,
                    name=name,
                    timeout=timeout_seconds,
                    waitForCompletion=wait_for_completion,
                )
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return process_info_from_api(response)

    def list(self) -> Sequence[SandboxProcessInfo]:
        """List the sandbox's processes."""
        try:
            response = self._api.get_process()
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return [process_info_from_api(process) for process in response.root]

    def logs(self, identifier: str) -> SandboxProcessLogs:
        """Get one process's captured output, by pid or name."""
        try:
            response = self._api.get_process_logs(identifier=identifier)
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return process_logs_from_api(response)


class AsyncSandboxProcess:
    """Processes in a sandbox. Async variant."""

    def __init__(self, api: baseten.client.sandboxapi.AsyncApiClient) -> None:
        self._api = api

    async def exec(
        self,
        command: str,
        *,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        name: str | None = None,
        timeout_seconds: int | None = None,
        wait_for_completion: bool | None = None,
    ) -> SandboxProcessInfo:
        """Start a command.

        Never retried, since a command may have side effects even when its
        response is lost.
        """
        try:
            response = await self._api.post_process(
                request=baseten.client.sandboxapi.ProcessRequest(
                    command=command,
                    workingDir=working_dir,
                    env=env,
                    name=name,
                    timeout=timeout_seconds,
                    waitForCompletion=wait_for_completion,
                )
            )
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return process_info_from_api(response)

    async def list(self) -> Sequence[SandboxProcessInfo]:
        """List the sandbox's processes."""
        try:
            response = await self._api.get_process()
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return [process_info_from_api(process) for process in response.root]

    async def logs(self, identifier: str) -> SandboxProcessLogs:
        """Get one process's captured output, by pid or name."""
        try:
            response = await self._api.get_process_logs(identifier=identifier)
        except Exception as error:
            raise to_sandbox_api_error(error, "exec") from error
        return process_logs_from_api(response)

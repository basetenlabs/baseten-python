from __future__ import annotations

import baseten.client.sandboxapi
from baseten.sandbox._common import AsyncSandboxContext, SandboxContext
from baseten.sandbox._fs import AsyncSandboxFileSystem, SandboxFileSystem
from baseten.sandbox._process import AsyncSandboxProcess, SandboxProcess


class Sandbox:
    """One sandbox, to run processes and work with files in.

    Get one from :meth:`SandboxClient.create`, :meth:`SandboxClient.get`, or
    :meth:`SandboxClient.sandbox_from_url`.
    """

    def __init__(self, context: SandboxContext) -> None:
        """Internal. Use the :class:`SandboxClient` methods that return one instead.

        :meta private:
        """
        self._context = context
        self._process = SandboxProcess(context)
        self._fs = SandboxFileSystem(context, self._process)

    @property
    def name(self) -> str:
        """Name of the sandbox. Empty when made from a URL alone."""
        return self._context.name

    @property
    def url(self) -> str:
        """Base URL of the sandbox's execution API."""
        return self._context.url

    @property
    def fs(self) -> SandboxFileSystem:
        """Files and directories in the sandbox."""
        return self._fs

    @property
    def process(self) -> SandboxProcess:
        """Processes in the sandbox."""
        return self._process

    @property
    def raw_api(self) -> baseten.client.sandboxapi.ApiClient:
        """The generated client for this sandbox's execution API, authenticated.

        For operations this SDK does not wrap. The generated API surface is not
        covered by stability guarantees and may change between versions.
        """
        return self._context.raw_api


class AsyncSandbox:
    """Async form of :class:`Sandbox`.

    Get one from :meth:`AsyncSandboxClient.create`,
    :meth:`AsyncSandboxClient.get`, or
    :meth:`AsyncSandboxClient.sandbox_from_url`.
    """

    def __init__(self, context: AsyncSandboxContext) -> None:
        """Internal. Use the :class:`AsyncSandboxClient` methods that return one instead.

        :meta private:
        """
        self._context = context
        self._process = AsyncSandboxProcess(context)
        self._fs = AsyncSandboxFileSystem(context, self._process)

    @property
    def name(self) -> str:
        """Name of the sandbox. Empty when made from a URL alone."""
        return self._context.name

    @property
    def url(self) -> str:
        """Base URL of the sandbox's execution API."""
        return self._context.url

    @property
    def fs(self) -> AsyncSandboxFileSystem:
        """Files and directories in the sandbox."""
        return self._fs

    @property
    def process(self) -> AsyncSandboxProcess:
        """Processes in the sandbox."""
        return self._process

    @property
    def raw_api(self) -> baseten.client.sandboxapi.AsyncApiClient:
        """The generated client for this sandbox's execution API, authenticated.

        For operations this SDK does not wrap. The generated API surface is not
        covered by stability guarantees and may change between versions.
        """
        return self._context.raw_api

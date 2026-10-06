from __future__ import annotations

import asyncio
import builtins
import concurrent.futures
import contextlib
import json
import posixpath
import threading
import urllib.parse
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

import httpx

import baseten.client.sandboxapi
from baseten.sandbox._common import (
    AsyncSandboxContext,
    AsyncSandboxStream,
    SandboxContext,
    SandboxStream,
    parse_timestamp,
)
from baseten.sandbox._errors import SandboxError
from baseten.sandbox._info import set_fields
from baseten.sandbox._process import (
    AsyncSandboxProcess,
    SandboxProcess,
    SandboxProcessInfo,
)
from baseten.sandbox._retry import retry_idempotent, retry_idempotent_async

_DEFAULT_COPY_TIMEOUT = timedelta(seconds=180)
_COPY_POLL_INTERVAL = timedelta(milliseconds=100)

_MULTIPART_THRESHOLD = 5 * 1024 * 1024
_MULTIPART_PART_SIZE = 5 * 1024 * 1024

# Bounds the abort of a failed multipart upload, which runs even when the
# caller gave up, so a stalled abort cannot hang the write it cleans up after.
_MULTIPART_ABORT_TIMEOUT = timedelta(seconds=10)

# Most upload parts in flight at once per sandbox, across all of its uploads.
# Many at once on one HTTP/2 connection can trip the server's rapid reset
# limit, so this holds on every transport rather than guessing which is used.
_UPLOAD_PARTS_IN_FLIGHT = 2

SandboxFileSystemEntryType: TypeAlias = Literal["file", "directory"] | str
"""Kind of a filesystem entry.

Other values may be added, so do not treat this list as exhaustive.
"""

SandboxFileSystemWatchOp: TypeAlias = (
    Literal["CREATE", "WRITE", "REMOVE", "RENAME", "CHMOD"] | str
)
"""A kind of change in a :class:`SandboxFileSystemWatchEvent`.

Other values may be added, so do not treat this list as exhaustive.
"""


class SandboxFileSystemCopyError(SandboxError):
    """A copy by :meth:`SandboxFileSystem.copy` that did not succeed."""

    source: str
    """Path copied from."""

    destination: str
    """Path copied to."""

    process: SandboxProcessInfo
    """The ``cp`` process as it ended, whose ``logs`` hold its error output."""

    def __init__(
        self, *, source: str, destination: str, process: SandboxProcessInfo
    ) -> None:
        """Create an error.

        Args:
            source: Path copied from.
            destination: Path copied to.
            process: The ``cp`` process as it ended.
        """
        detail = process.stderr.strip() or f"exit code {process.exit_code}"
        super().__init__(
            f"copying {source} to {destination} ended {process.status}: {detail}"
        )
        self.source = source
        self.destination = destination
        self.process = process


@dataclass(kw_only=True)
class SandboxFileSystemFileInfo:
    """A file in a :class:`SandboxFileSystemDirectory`."""

    name: str

    path: str

    size_bytes: int

    permissions: str
    """File mode, as the sandbox reports it."""

    owner: str

    group: str

    last_modified: datetime


@dataclass(kw_only=True)
class SandboxFileSystemSubdirectoryInfo:
    """A subdirectory in a :class:`SandboxFileSystemDirectory`."""

    name: str

    path: str


@dataclass(kw_only=True)
class SandboxFileSystemDirectory:
    """A directory's contents, one level deep, from :meth:`SandboxFileSystem.list`."""

    name: str

    path: str

    files: list[SandboxFileSystemFileInfo]

    subdirectories: list[SandboxFileSystemSubdirectoryInfo]


@dataclass(kw_only=True)
class SandboxFileSystemFindMatch:
    """An entry found by :meth:`SandboxFileSystem.find`."""

    path: str
    """Path relative to the searched path."""

    type: SandboxFileSystemEntryType


@dataclass(kw_only=True)
class SandboxFileSystemFindResult:
    """Result of :meth:`SandboxFileSystem.find`."""

    matches: list[SandboxFileSystemFindMatch]

    total: int
    """Number of entries found, which may exceed the matches returned."""


@dataclass(kw_only=True)
class SandboxFileSystemGrepMatch:
    """A matching line found by :meth:`SandboxFileSystem.grep`."""

    path: str
    """Path relative to the searched path."""

    line: int
    """Line number, starting at 1."""

    column: int

    text: str
    """The matching line."""

    context: str | None
    """The matching line with the lines around it, newline-separated.

    Holds up to ``context_lines`` lines before and after the match, and is
    ``None`` when ``context_lines`` was not set.
    """


@dataclass(kw_only=True)
class SandboxFileSystemGrepResult:
    """Result of :meth:`SandboxFileSystem.grep`."""

    matches: list[SandboxFileSystemGrepMatch]

    total: int
    """Number of matching lines, which may exceed the matches returned."""


@dataclass(kw_only=True)
class SandboxFileSystemWatchEvent:
    """A change seen by :meth:`SandboxFileSystem.watch`."""

    ops: list[SandboxFileSystemWatchOp]
    """Kinds of change, more than one when the sandbox reports several together."""

    path: str
    """Full path of the changed entry."""


class SandboxFileSystem:
    """Files and directories in a sandbox.

    Relative paths are resolved against the sandbox's working directory.
    """

    def __init__(self, context: SandboxContext, process: SandboxProcess) -> None:
        """Internal. Obtained from :attr:`Sandbox.fs`.

        :meta private:
        """
        self._context = context
        self._process = process
        # One per sandbox, since a sandbox builds its file system once.
        self._part_slots = threading.Semaphore(_UPLOAD_PARTS_IN_FLIGHT)
        self._executor_lock = threading.Lock()
        self._own_executor: concurrent.futures.ThreadPoolExecutor | None = None

    def read(self, *, path: str) -> str:
        """Read a text file.

        Args:
            path: Path of the file.
        """
        result = self._context.call_read(lambda api: api.get_filesystem(path=path))
        return _file_content(path, result)

    def read_bytes(self, *, path: str) -> bytes:
        """Read a file as bytes.

        Args:
            path: Path of the file.
        """

        # The body is read inside the retried call, so a connection dropped
        # partway through the body is retried too.
        def read(api: baseten.client.sandboxapi.ApiClient) -> bytes:
            response = api.get_filesystem_raw(
                path=path, accept="application/octet-stream"
            )
            try:
                _check_not_directory(path, response)
                return response.read()
            finally:
                response.close()

        return self._context.call_read(read)

    def write(self, *, path: str, content: str) -> None:
        """Write a text file, creating it or replacing its content.

        Content over 5MB is uploaded in 5MB parts through the multipart upload
        endpoints, at most 2 parts at a time per sandbox.

        Args:
            path: Path of the file, created or replaced.
            content: Text content to write.
        """
        encoded = _encoded_if_large(content)
        if encoded is not None:
            self._write_multipart(path, encoded, None)
            return
        # Retried, since the spec documents the write as idempotent.
        self._context.call_upload(
            lambda api: api.put_filesystem(
                path=path,
                request=baseten.client.sandboxapi.FileRequest(content=content),
            )
        )

    def write_bytes(
        self, *, path: str, content: bytes, permissions: str | None = None
    ) -> None:
        """Write a file from bytes, creating it or replacing its content.

        Content over 5MB is uploaded in 5MB parts through the multipart upload
        endpoints, at most 2 parts at a time per sandbox.

        Args:
            path: Path of the file, created or replaced.
            content: Content to write.
            permissions: Octal file mode, such as ``"0755"``, applied when the
                file is created. An existing file keeps its mode. ``None``
                uses the sandbox's default.
        """
        if len(content) > _MULTIPART_THRESHOLD:
            self._write_multipart(path, content, permissions)
            return
        # The generated client has no multipart form for this endpoint, since
        # it also takes a JSON body. Retried, since the spec documents the
        # write as idempotent, with the request rebuilt for each attempt.
        retry_idempotent(
            lambda: self._context.send(
                _write_bytes_request(self._context, path, content, permissions)
            ),
            max_retries=self._context.retry.upload_max_retries,
            gateway_max_retries=self._context.retry.gateway_max_retries,
        )

    def mkdir(self, *, path: str, permissions: str | None = None) -> None:
        """Create a directory, along with any missing parents.

        Args:
            path: Path of the directory.
            permissions: Octal directory mode, such as ``"0755"``. ``None``
                uses the sandbox's default.
        """
        # Retried, since the spec documents the write as idempotent.
        self._context.call_upload(
            lambda api: api.put_filesystem(
                path=path, request=_mkdir_request(permissions)
            )
        )

    def list(self, *, path: str) -> SandboxFileSystemDirectory:
        """List a directory's files and subdirectories, one level deep.

        Args:
            path: Path of the directory.
        """
        result = self._context.call_read(lambda api: api.get_filesystem(path=path))
        return _directory(path, result)

    def remove(self, *, path: str, recursive: bool | None = None) -> None:
        """Remove a file or directory.

        Args:
            path: Path of the file or directory.
            recursive: Whether to remove a directory and everything in it.
                A directory that is not empty fails without it.
        """
        # Not retried, since a repeat of a removal whose response was lost
        # would fail for a path that is already gone.
        with self._context.api() as api:
            api.delete_filesystem(
                path=path,
                params=baseten.client.sandboxapi.DeleteFilesystemParams(
                    **set_fields(recursive=recursive)
                ),
            )

    def find(
        self,
        *,
        path: str,
        type: SandboxFileSystemEntryType | None = None,
        patterns: builtins.list[str] | None = None,
        max_results: int | None = None,
        exclude_dirs: builtins.list[str] | None = None,
        exclude_hidden: bool | None = None,
    ) -> SandboxFileSystemFindResult:
        """Find files and directories by name under a directory.

        Args:
            path: Path of the directory to search in.
            type: Whether to find only files or only directories. ``None``
                finds both.
            patterns: File patterns to include, such as ``"*.py"``.
            max_results: Maximum number of results. 0 means all. The sandbox
                defaults to 20.
            exclude_dirs: Directory names to skip. The sandbox defaults to
                common dependency and build directories, such as
                ``node_modules`` and ``.git``.
            exclude_hidden: Whether to skip hidden files and directories. The
                sandbox defaults to true.
        """
        params = _find_params(type, patterns, max_results, exclude_dirs, exclude_hidden)
        result = self._context.call_read(
            lambda api: api.get_filesystem_find(path=path, params=params)
        )
        return _find_result(result)

    def grep(
        self,
        *,
        path: str,
        query: str,
        case_sensitive: bool | None = None,
        max_results: int | None = None,
        file_pattern: str | None = None,
        exclude_dirs: builtins.list[str] | None = None,
        context_lines: int | None = None,
    ) -> SandboxFileSystemGrepResult:
        """Search the contents of files under a directory for text.

        Args:
            path: Path of the directory to search in.
            query: Text to search for.
            case_sensitive: Whether the search is case sensitive. The sandbox
                defaults to false.
            max_results: Maximum number of results. The sandbox defaults to
                100.
            file_pattern: File pattern to include, such as ``"*.py"``.
            exclude_dirs: Directory names to skip. The sandbox defaults to
                common dependency and build directories, such as
                ``node_modules`` and ``.git``.
            context_lines: How many lines before and after each match to
                include in its :attr:`SandboxFileSystemGrepMatch.context`, at
                most 20. ``None`` includes none.
        """
        params = _grep_params(
            query,
            case_sensitive,
            max_results,
            file_pattern,
            exclude_dirs,
            context_lines,
        )
        result = self._context.call_read(
            lambda api: api.get_filesystem_content_search(path=path, params=params)
        )
        return _grep_result(result)

    def copy(
        self,
        *,
        source: str,
        destination: str,
        timeout: timedelta | None = _DEFAULT_COPY_TIMEOUT,
    ) -> None:
        """Copy a file or directory.

        Runs ``cp -r`` in the sandbox through :meth:`SandboxProcess.exec`, then
        waits for it with :meth:`SandboxProcess.wait`, so ``cp``'s rules apply,
        such as copying into a destination that is an existing directory.

        Args:
            source: Path of the file or directory to copy.
            destination: Path to copy to.
            timeout: How long to wait for the copy, which keeps running
                regardless. ``None`` waits with no limit.

        Raises:
            SandboxFileSystemCopyError: The copy did not succeed.
            SandboxProcessWaitTimeoutError: The copy was still running when
                the wait timed out.
        """
        started = self._process.exec(command=_copy_command(source, destination))
        finished = self._process.wait(
            identifier=started.pid, timeout=timeout, poll_interval=_COPY_POLL_INTERVAL
        )
        if finished.status != "completed" or finished.exit_code != 0:
            raise SandboxFileSystemCopyError(
                source=source, destination=destination, process=finished
            )

    def write_tree(self, *, path: str, files: Mapping[str, str]) -> None:
        """Write several text files under a directory in one call, creating or replacing each.

        Args:
            path: Path of the directory to write under.
            files: Text content of each file, by path relative to *path*.
        """
        # Retried, since the spec documents the write as idempotent.
        self._context.call_upload(
            lambda api: api.put_filesystem_tree(
                path=path,
                request=baseten.client.sandboxapi.TreeRequest(files=dict(files)),
            )
        )

    def watch(
        self,
        *,
        path: str,
        recursive: bool | None = None,
        ignore: builtins.list[str] | None = None,
    ) -> SandboxStream[SandboxFileSystemWatchEvent]:
        """Stream changes in a directory as they happen.

        Only the directory's direct entries are watched unless *recursive* is
        set. Closing the stream stops watching.

        Args:
            path: Path of the directory to watch.
            recursive: Whether to also watch every subdirectory, including
                ones created later.
            ignore: Skips events whose full path contains any of these
                substrings.
        """
        # Not retried, since a resent watch would miss changes in between.
        with self._context.api() as api:
            response = api.get_watch_filesystem(
                path=_watch_path(path, recursive), params=_watch_params(ignore)
            )
        return SandboxStream(response, _watch_events)

    def _write_multipart(
        self, path: str, content: bytes, permissions: str | None
    ) -> None:
        """Upload content in parts through the multipart upload endpoints."""
        # Not retried, since a repeat would start a second upload.
        with self._context.api() as api:
            initiated = api.post_filesystem_multipart_initiate(
                path=path, request=_multipart_initiate_request(permissions)
            )
        upload_id = _upload_id(path, initiated)
        try:
            parts = self._send_parts(upload_id, content)
            # Not retried, since a repeat of a completion whose response was
            # lost would fail for an upload that is already complete.
            with self._context.api() as api:
                api.post_filesystem_multipart_complete(
                    upload_id=upload_id,
                    request=baseten.client.sandboxapi.MultipartCompleteRequest(
                        parts=parts
                    ),
                )
        except BaseException:
            # Sent even on an interrupt, which may be what ended the upload.
            # Its own failure is dropped in favor of the error that ended it.
            with (
                contextlib.suppress(Exception),
                self._context.api_with_timeout(_MULTIPART_ABORT_TIMEOUT) as api,
            ):
                api.delete_filesystem_multipart_abort(upload_id=upload_id)
            raise

    def _send_parts(
        self, upload_id: str, content: bytes
    ) -> builtins.list[baseten.client.sandboxapi.MultipartPartInfo]:
        """Upload every part of content. The first part to fail stops the rest."""
        part_count = -(-len(content) // _MULTIPART_PART_SIZE)
        parts: builtins.list[baseten.client.sandboxapi.MultipartPartInfo | None] = [
            None
        ] * part_count
        lock = threading.Lock()
        next_index = 0
        stopped = threading.Event()

        def work() -> None:
            nonlocal next_index
            while True:
                with lock:
                    if stopped.is_set() or next_index >= part_count:
                        return
                    index = next_index
                    next_index += 1
                part_number = index + 1
                try:
                    etag = self._send_part(
                        upload_id,
                        part_number,
                        content[
                            index * _MULTIPART_PART_SIZE : part_number
                            * _MULTIPART_PART_SIZE
                        ],
                    )
                except BaseException:
                    stopped.set()
                    raise
                parts[index] = baseten.client.sandboxapi.MultipartPartInfo(
                    partNumber=part_number, etag=etag
                )

        # The calling thread is one of the workers, so a caller's pool that is
        # busy, even with this very call, slows the upload rather than
        # deadlocking it.
        other = self._executor().submit(work) if part_count > 1 else None
        try:
            work()
        except BaseException:
            # Lets none outlive this call. One that never started is
            # cancelled instead, since it may be queued behind this very call.
            if other is not None and not other.cancel():
                concurrent.futures.wait([other])
            raise
        # The calling thread sent every part when the other worker never
        # started.
        if other is not None and not other.cancel():
            try:
                other.result()
            except BaseException:
                # Also when interrupted while waiting, so the other worker
                # stops after its current part.
                stopped.set()
                raise
        return [part for part in parts if part is not None]

    def _send_part(self, upload_id: str, part_number: int, part: bytes) -> str | None:
        """Upload one part and return its ETag."""

        # The slot is taken per attempt, so a part waiting to retry does not
        # hold one.
        def send(
            api: baseten.client.sandboxapi.ApiClient,
        ) -> baseten.client.sandboxapi.MultipartUploadPartResponse:
            with self._part_slots:
                return api.put_filesystem_multipart_part(
                    upload_id=upload_id,
                    params=baseten.client.sandboxapi.PutFilesystemMultipartPartParams(
                        partNumber=part_number
                    ),
                    # The filename a browser's FormData gives a part sent
                    # without one.
                    files={"file": ("blob", part)},
                )

        # Retried, since a repeat sends the same part.
        return self._context.call_upload(send).etag

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Give the caller's thread pool, or this sandbox's own, created on first use."""
        if self._context.executor is not None:
            return self._context.executor
        with self._executor_lock:
            if self._own_executor is None:
                # Its idle threads end once this sandbox is no longer
                # referenced, since the pool is then collected.
                self._own_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_UPLOAD_PARTS_IN_FLIGHT,
                    thread_name_prefix="baseten-sandbox-upload",
                )
            return self._own_executor


class AsyncSandboxFileSystem:
    """Async form of :class:`SandboxFileSystem`."""

    def __init__(
        self, context: AsyncSandboxContext, process: AsyncSandboxProcess
    ) -> None:
        """Internal. Obtained from :attr:`AsyncSandbox.fs`.

        :meta private:
        """
        self._context = context
        self._process = process
        # One per sandbox, since a sandbox builds its file system once.
        self._part_slots = asyncio.Semaphore(_UPLOAD_PARTS_IN_FLIGHT)

    async def read(self, *, path: str) -> str:
        """Read a text file. As :meth:`SandboxFileSystem.read`."""
        result = await self._context.call_read(
            lambda api: api.get_filesystem(path=path)
        )
        return _file_content(path, result)

    async def read_bytes(self, *, path: str) -> bytes:
        """Read a file as bytes. As :meth:`SandboxFileSystem.read_bytes`."""

        async def read(api: baseten.client.sandboxapi.AsyncApiClient) -> bytes:
            response = await api.get_filesystem_raw(
                path=path, accept="application/octet-stream"
            )
            try:
                _check_not_directory(path, response)
                return await response.aread()
            finally:
                await response.aclose()

        return await self._context.call_read(read)

    async def write(self, *, path: str, content: str) -> None:
        """Write a text file. As :meth:`SandboxFileSystem.write`."""
        encoded = _encoded_if_large(content)
        if encoded is not None:
            await self._write_multipart(path, encoded, None)
            return
        await self._context.call_upload(
            lambda api: api.put_filesystem(
                path=path,
                request=baseten.client.sandboxapi.FileRequest(content=content),
            )
        )

    async def write_bytes(
        self, *, path: str, content: bytes, permissions: str | None = None
    ) -> None:
        """Write a file from bytes. As :meth:`SandboxFileSystem.write_bytes`."""
        if len(content) > _MULTIPART_THRESHOLD:
            await self._write_multipart(path, content, permissions)
            return
        await retry_idempotent_async(
            lambda: self._context.send(
                _write_bytes_request(self._context, path, content, permissions)
            ),
            max_retries=self._context.retry.upload_max_retries,
            gateway_max_retries=self._context.retry.gateway_max_retries,
        )

    async def mkdir(self, *, path: str, permissions: str | None = None) -> None:
        """Create a directory. As :meth:`SandboxFileSystem.mkdir`."""
        await self._context.call_upload(
            lambda api: api.put_filesystem(
                path=path, request=_mkdir_request(permissions)
            )
        )

    async def list(self, *, path: str) -> SandboxFileSystemDirectory:
        """List a directory. As :meth:`SandboxFileSystem.list`."""
        result = await self._context.call_read(
            lambda api: api.get_filesystem(path=path)
        )
        return _directory(path, result)

    async def remove(self, *, path: str, recursive: bool | None = None) -> None:
        """Remove a file or directory. As :meth:`SandboxFileSystem.remove`."""
        with self._context.api() as api:
            await api.delete_filesystem(
                path=path,
                params=baseten.client.sandboxapi.DeleteFilesystemParams(
                    **set_fields(recursive=recursive)
                ),
            )

    async def find(
        self,
        *,
        path: str,
        type: SandboxFileSystemEntryType | None = None,
        patterns: builtins.list[str] | None = None,
        max_results: int | None = None,
        exclude_dirs: builtins.list[str] | None = None,
        exclude_hidden: bool | None = None,
    ) -> SandboxFileSystemFindResult:
        """Find files and directories by name. As :meth:`SandboxFileSystem.find`."""
        params = _find_params(type, patterns, max_results, exclude_dirs, exclude_hidden)
        result = await self._context.call_read(
            lambda api: api.get_filesystem_find(path=path, params=params)
        )
        return _find_result(result)

    async def grep(
        self,
        *,
        path: str,
        query: str,
        case_sensitive: bool | None = None,
        max_results: int | None = None,
        file_pattern: str | None = None,
        exclude_dirs: builtins.list[str] | None = None,
        context_lines: int | None = None,
    ) -> SandboxFileSystemGrepResult:
        """Search the contents of files. As :meth:`SandboxFileSystem.grep`."""
        params = _grep_params(
            query,
            case_sensitive,
            max_results,
            file_pattern,
            exclude_dirs,
            context_lines,
        )
        result = await self._context.call_read(
            lambda api: api.get_filesystem_content_search(path=path, params=params)
        )
        return _grep_result(result)

    async def copy(
        self,
        *,
        source: str,
        destination: str,
        timeout: timedelta | None = _DEFAULT_COPY_TIMEOUT,
    ) -> None:
        """Copy a file or directory. As :meth:`SandboxFileSystem.copy`."""
        started = await self._process.exec(command=_copy_command(source, destination))
        finished = await self._process.wait(
            identifier=started.pid, timeout=timeout, poll_interval=_COPY_POLL_INTERVAL
        )
        if finished.status != "completed" or finished.exit_code != 0:
            raise SandboxFileSystemCopyError(
                source=source, destination=destination, process=finished
            )

    async def write_tree(self, *, path: str, files: Mapping[str, str]) -> None:
        """Write several text files. As :meth:`SandboxFileSystem.write_tree`."""
        await self._context.call_upload(
            lambda api: api.put_filesystem_tree(
                path=path,
                request=baseten.client.sandboxapi.TreeRequest(files=dict(files)),
            )
        )

    async def watch(
        self,
        *,
        path: str,
        recursive: bool | None = None,
        ignore: builtins.list[str] | None = None,
    ) -> AsyncSandboxStream[SandboxFileSystemWatchEvent]:
        """Stream changes in a directory. As :meth:`SandboxFileSystem.watch`."""
        with self._context.api() as api:
            response = await api.get_watch_filesystem(
                path=_watch_path(path, recursive), params=_watch_params(ignore)
            )
        return AsyncSandboxStream(response, _watch_events_async)

    async def _write_multipart(
        self, path: str, content: bytes, permissions: str | None
    ) -> None:
        """Upload content in parts through the multipart upload endpoints."""
        with self._context.api() as api:
            initiated = await api.post_filesystem_multipart_initiate(
                path=path, request=_multipart_initiate_request(permissions)
            )
        upload_id = _upload_id(path, initiated)
        try:
            parts = await self._send_parts(upload_id, content)
            with self._context.api() as api:
                await api.post_filesystem_multipart_complete(
                    upload_id=upload_id,
                    request=baseten.client.sandboxapi.MultipartCompleteRequest(
                        parts=parts
                    ),
                )
        except BaseException:
            # Shielded, so the caller's cancellation, which may be what ended
            # the upload, does not stop it. Its own failure is dropped in
            # favor of the error that ended the upload.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._abort_multipart(upload_id))
            raise

    async def _abort_multipart(self, upload_id: str) -> None:
        """Abort a multipart upload, on its own timeout."""
        with self._context.api_with_timeout(_MULTIPART_ABORT_TIMEOUT) as api:
            await api.delete_filesystem_multipart_abort(upload_id=upload_id)

    async def _send_parts(
        self, upload_id: str, content: bytes
    ) -> builtins.list[baseten.client.sandboxapi.MultipartPartInfo]:
        """Upload every part of content. The first part to fail stops the rest."""
        part_count = -(-len(content) // _MULTIPART_PART_SIZE)
        parts: builtins.list[baseten.client.sandboxapi.MultipartPartInfo | None] = [
            None
        ] * part_count
        next_index = 0

        async def work() -> None:
            nonlocal next_index
            while next_index < part_count:
                index = next_index
                next_index += 1
                part_number = index + 1
                etag = await self._send_part(
                    upload_id,
                    part_number,
                    content[
                        index * _MULTIPART_PART_SIZE : part_number
                        * _MULTIPART_PART_SIZE
                    ],
                )
                parts[index] = baseten.client.sandboxapi.MultipartPartInfo(
                    partNumber=part_number, etag=etag
                )

        # More workers than slots would only wait for one.
        workers = [
            asyncio.create_task(work())
            for _ in range(min(_UPLOAD_PARTS_IN_FLIGHT, part_count))
        ]
        try:
            await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # Stops the rest after a failure or the caller's cancellation, and
            # lets none outlive this call.
            for worker in workers:
                worker.cancel()
            await asyncio.wait(workers)
        for worker in workers:
            if not worker.cancelled() and (error := worker.exception()) is not None:
                raise error
        return [part for part in parts if part is not None]

    async def _send_part(
        self, upload_id: str, part_number: int, part: bytes
    ) -> str | None:
        """Upload one part and return its ETag."""

        async def send(
            api: baseten.client.sandboxapi.AsyncApiClient,
        ) -> baseten.client.sandboxapi.MultipartUploadPartResponse:
            async with self._part_slots:
                return await api.put_filesystem_multipart_part(
                    upload_id=upload_id,
                    params=baseten.client.sandboxapi.PutFilesystemMultipartPartParams(
                        partNumber=part_number
                    ),
                    files={"file": ("blob", part)},
                )

        return (await self._context.call_upload(send)).etag


def _file_content(
    path: str, result: baseten.client.sandboxapi.GetFilesystemResponse
) -> str:
    """Give a file's text from a filesystem read, failing for a directory."""
    entry = result.root
    if isinstance(entry, baseten.client.sandboxapi.FileWithContent):
        return entry.content
    if isinstance(entry, bytes):
        return entry.decode(errors="replace")
    raise SandboxError(f"{path} is a directory, not a file")


def _check_not_directory(path: str, response: httpx.Response) -> None:
    """Fail when a byte read returned a directory listing."""
    # A directory comes back as its JSON listing whatever was asked for.
    if "application/json" in response.headers.get("content-type", ""):
        raise SandboxError(f"{path} is a directory, not a file")


def _encoded_if_large(content: str) -> bytes | None:
    """Give text as UTF-8 if that is over the multipart threshold, else ``None``."""
    # A code point is at most 4 bytes of UTF-8, so only a long string can be
    # over the threshold, and shorter ones skip encoding just to measure.
    if len(content) * 4 <= _MULTIPART_THRESHOLD:
        return None
    encoded = content.encode()
    return encoded if len(encoded) > _MULTIPART_THRESHOLD else None


def _write_bytes_request(
    context: SandboxContext | AsyncSandboxContext,
    path: str,
    content: bytes,
    permissions: str | None,
) -> httpx.Request:
    """Build the multipart form request that writes a file from bytes."""
    data = {"permissions": permissions} if permissions is not None else {}
    data["path"] = path
    return context.build_request(
        "PUT",
        "/filesystem/" + urllib.parse.quote(path, safe=""),
        files={"file": (posixpath.basename(path) or "file", content)},
        data=data,
    )


def _mkdir_request(permissions: str | None) -> baseten.client.sandboxapi.FileRequest:
    """Build the request that creates a directory."""
    return baseten.client.sandboxapi.FileRequest(
        **set_fields(isDirectory=True, permissions=permissions)
    )


def _directory(
    path: str, result: baseten.client.sandboxapi.GetFilesystemResponse
) -> SandboxFileSystemDirectory:
    """Give a directory's listing from a filesystem read, failing for a file."""
    entry = result.root
    if not isinstance(entry, baseten.client.sandboxapi.Directory):
        raise SandboxError(f"{path} is a file, not a directory")
    return SandboxFileSystemDirectory(
        name=entry.name,
        path=entry.path,
        files=[
            SandboxFileSystemFileInfo(
                name=file.name,
                path=file.path,
                size_bytes=file.size,
                permissions=file.permissions,
                owner=file.owner,
                group=file.group,
                last_modified=parse_timestamp(
                    file.lastModified, "file modification time"
                ),
            )
            for file in entry.files
        ],
        subdirectories=[
            SandboxFileSystemSubdirectoryInfo(
                name=subdirectory.name, path=subdirectory.path
            )
            for subdirectory in entry.subdirectories
        ],
    )


def _find_params(
    type: str | None,
    patterns: builtins.list[str] | None,
    max_results: int | None,
    exclude_dirs: builtins.list[str] | None,
    exclude_hidden: bool | None,
) -> baseten.client.sandboxapi.GetFilesystemFindParams:
    """Build the query of a find."""
    return baseten.client.sandboxapi.GetFilesystemFindParams(
        **set_fields(
            type=type,
            patterns=None if patterns is None else ",".join(patterns),
            maxResults=max_results,
            excludeDirs=None if exclude_dirs is None else ",".join(exclude_dirs),
            excludeHidden=exclude_hidden,
        )
    )


def _find_result(
    result: baseten.client.sandboxapi.FindResponse,
) -> SandboxFileSystemFindResult:
    """Convert a find's response."""
    return SandboxFileSystemFindResult(
        matches=[
            SandboxFileSystemFindMatch(path=match.path, type=match.type)
            for match in result.matches
        ],
        total=result.total,
    )


def _grep_params(
    query: str,
    case_sensitive: bool | None,
    max_results: int | None,
    file_pattern: str | None,
    exclude_dirs: builtins.list[str] | None,
    context_lines: int | None,
) -> baseten.client.sandboxapi.GetFilesystemContentSearchParams:
    """Build the query of a content search."""
    return baseten.client.sandboxapi.GetFilesystemContentSearchParams(
        query=query,
        **set_fields(
            caseSensitive=case_sensitive,
            maxResults=max_results,
            filePattern=file_pattern,
            excludeDirs=None if exclude_dirs is None else ",".join(exclude_dirs),
            contextLines=context_lines,
        ),
    )


def _grep_result(
    result: baseten.client.sandboxapi.ContentSearchResponse,
) -> SandboxFileSystemGrepResult:
    """Convert a content search's response."""
    return SandboxFileSystemGrepResult(
        matches=[
            SandboxFileSystemGrepMatch(
                path=match.path,
                line=match.line,
                column=match.column,
                text=match.text,
                context=match.context,
            )
            for match in result.matches
        ],
        total=result.total,
    )


def _copy_command(source: str, destination: str) -> str:
    """Build the shell command that copies source to destination."""
    # Each path quoted as one literal POSIX shell argument.
    quoted = [
        "'" + value.replace("'", "'\\''") + "'" for value in (source, destination)
    ]
    return f"cp -r {quoted[0]} {quoted[1]}"


def _watch_path(path: str, recursive: bool | None) -> str:
    """Give the watch endpoint's path, with the suffix that makes it recursive."""
    return path.rstrip("/") + "/**" if recursive else path


def _watch_params(
    ignore: builtins.list[str] | None,
) -> baseten.client.sandboxapi.GetWatchFilesystemParams:
    """Build the query of a watch."""
    return baseten.client.sandboxapi.GetWatchFilesystemParams(
        **set_fields(ignore=None if ignore is None else ",".join(ignore))
    )


def _watch_event(line: str) -> SandboxFileSystemWatchEvent | None:
    """Parse one line of a watch, or ``None`` for a line to skip."""
    if line.strip() == "" or line.startswith("[keepalive]"):
        return None
    event = json.loads(line)
    directory = event["path"]
    return SandboxFileSystemWatchEvent(
        ops=event["op"].split("|"),
        path=directory.rstrip("/") + "/" + event["name"],
    )


def _watch_events(
    lines: Iterator[str],
) -> Generator[SandboxFileSystemWatchEvent, None, None]:
    """Parse a watch into events."""
    for line in lines:
        event = _watch_event(line)
        if event is not None:
            yield event


async def _watch_events_async(
    lines: AsyncIterator[str],
) -> AsyncGenerator[SandboxFileSystemWatchEvent, None]:
    """Async form of :func:`_watch_events`."""
    async for line in lines:
        event = _watch_event(line)
        if event is not None:
            yield event


def _multipart_initiate_request(
    permissions: str | None,
) -> baseten.client.sandboxapi.MultipartInitiateRequest:
    """Build the request that starts a multipart upload."""
    return baseten.client.sandboxapi.MultipartInitiateRequest(
        **set_fields(permissions=permissions)
    )


def _upload_id(
    path: str, initiated: baseten.client.sandboxapi.MultipartInitiateResponse
) -> str:
    """Give a started multipart upload's ID, failing without one."""
    if not initiated.uploadId:
        raise SandboxError(f"multipart upload of {path} returned no upload ID")
    return initiated.uploadId

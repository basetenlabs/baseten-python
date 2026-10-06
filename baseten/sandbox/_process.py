from __future__ import annotations

import asyncio
import builtins
import json
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

import baseten.client.sandboxapi
from baseten.sandbox._common import (
    AsyncSandboxContext,
    AsyncSandboxStream,
    SandboxContext,
    SandboxStream,
    parse_timestamp,
)
from baseten.sandbox._errors import (
    SandboxAPIError,
    SandboxError,
    SandboxProcessWaitTimeoutError,
)
from baseten.sandbox._info import set_fields
from baseten.sandbox._retry import is_transient_reset_error

_DEFAULT_WAIT_TIMEOUT = timedelta(seconds=60)
_DEFAULT_WAIT_POLL_INTERVAL = timedelta(seconds=1)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

# Statuses a wait rides out, since the sandbox or its edge may briefly fail.
_WAIT_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

SandboxProcessStatus: TypeAlias = (
    Literal["running", "completed", "failed", "killed", "stopped"] | str
)
"""Status of a process in a sandbox.

Other values may be added, so do not treat this list as exhaustive.
"""


@dataclass(kw_only=True)
class SandboxProcessInfo:
    """A process in a sandbox."""

    pid: str
    """Process identifier, usable wherever a process name is."""

    name: str

    command: str

    status: SandboxProcessStatus

    exit_code: int

    stdout: str

    stderr: str

    logs: str
    """Standard output and standard error, interleaved."""

    working_dir: str

    started_at: datetime

    completed_at: datetime | None
    """When the process exited. ``None`` while it runs."""

    keep_alive: bool | None
    """Whether scale-to-zero is disabled for this process."""

    max_restarts: int | None
    """Maximum number of restarts on failure. Negative means unlimited."""

    restart_count: int | None
    """Number of times the process has been restarted."""

    restart_on_failure: bool | None
    """Whether the process is restarted when it fails."""

    stdin: bool | None
    """Whether the process was started with a writable stdin pipe."""


@dataclass(kw_only=True)
class SandboxProcessLogs:
    """Output of a process, as returned by :meth:`SandboxProcess.logs`."""

    stdout: str

    stderr: str

    logs: str
    """Standard output and standard error, interleaved."""


@dataclass(kw_only=True)
class SandboxProcessLogLine:
    """One line of a process's output."""

    stream: Literal["stdout", "stderr"] | None
    """Which output the line came from, when the sandbox reports it."""

    text: str
    """The line, without its line ending."""


@dataclass(kw_only=True)
class SandboxProcessExecEventOutput:
    """Output from :meth:`SandboxProcess.exec_stream`.

    As the process wrote it, which may hold several lines, part of one, or
    text with no newline at all, such as a prompt. If the process finished
    before streaming started, each line instead arrives as its own event,
    without its newline.
    """

    stream: Literal["stdout", "stderr"]
    """Which output the text came from."""

    text: str


@dataclass(kw_only=True)
class SandboxProcessExecEventExit:
    """The exit of the process run by :meth:`SandboxProcess.exec_stream`.

    Always the last event.
    """

    process: SandboxProcessInfo
    """The process as it exited."""


SandboxProcessExecEvent: TypeAlias = (
    SandboxProcessExecEventOutput | SandboxProcessExecEventExit
)
"""An event from :meth:`SandboxProcess.exec_stream`."""


class SandboxProcess:
    """Processes in a sandbox."""

    def __init__(self, context: SandboxContext) -> None:
        """Internal. Obtained from :attr:`Sandbox.process`.

        :meta private:
        """
        self._context = context

    def exec(
        self,
        *,
        command: str,
        name: str | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: timedelta | None = None,
        wait_for_completion: bool | None = None,
        keep_alive: bool | None = None,
        max_restarts: int | None = None,
        restart_on_failure: bool | None = None,
        stdin: bool | None = None,
        wait_for_ports: Sequence[int] | None = None,
    ) -> SandboxProcessInfo:
        """Start a command.

        Args:
            command: Shell command to run.
            name: Name to refer to the process by, instead of its PID.
            working_dir: Directory to run the command in.
            env: Environment variables for the process, on top of the
                sandbox's own.
            timeout: How long until the process is killed, rounded up to
                whole seconds. Zero means never. When *keep_alive* is true,
                defaults to 600 seconds.
            wait_for_completion: Whether to return only once the process has
                exited. Defaults to false.
            keep_alive: Disable scale-to-zero while the process runs.
            max_restarts: Maximum number of restarts on failure. A negative
                value, such as -1, means unlimited.
            restart_on_failure: Whether to restart the process when it fails.
            stdin: Open a writable stdin pipe, fed by :meth:`write_stdin` and
                closed by :meth:`close_stdin`. The pipe does not survive a
                restart of the sandbox's execution API: the process then
                reads end of file.
            wait_for_ports: Ports the process must be listening on before
                the call returns.
        """
        # Not retried, since a command may have side effects even when its
        # response is lost.
        with self._context.api() as api:
            process = api.post_process(
                request=_process_request(
                    command=command,
                    name=name,
                    working_dir=working_dir,
                    env=env,
                    timeout=timeout,
                    wait_for_completion=wait_for_completion,
                    keep_alive=keep_alive,
                    max_restarts=max_restarts,
                    restart_on_failure=restart_on_failure,
                    stdin=stdin,
                    wait_for_ports=wait_for_ports,
                )
            )
        return process_info_from_api(process)

    def exec_stream(
        self,
        *,
        command: str,
        name: str | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: timedelta | None = None,
        keep_alive: bool | None = None,
        max_restarts: int | None = None,
        restart_on_failure: bool | None = None,
        stdin: bool | None = None,
        wait_for_ports: Sequence[int] | None = None,
    ) -> SandboxStream[SandboxProcessExecEvent]:
        """Run a command, streaming its output as it arrives and then its exit.

        Closing the stream early stops only the stream, not the process.

        Args:
            command: Shell command to run.
            name: Name to refer to the process by, instead of its PID.
            working_dir: Directory to run the command in.
            env: Environment variables for the process, on top of the
                sandbox's own.
            timeout: How long until the process is killed, rounded up to
                whole seconds. Zero means never. When *keep_alive* is true,
                defaults to 600 seconds.
            keep_alive: Disable scale-to-zero while the process runs.
            max_restarts: Maximum number of restarts on failure. A negative
                value, such as -1, means unlimited.
            restart_on_failure: Whether to restart the process when it fails.
            stdin: Open a writable stdin pipe, fed by :meth:`write_stdin` and
                closed by :meth:`close_stdin`. The pipe does not survive a
                restart of the sandbox's execution API: the process then
                reads end of file.
            wait_for_ports: Ports the process must be listening on before
                output starts.
        """
        with self._context.api() as api:
            response = api.post_process_raw(
                accept="application/x-ndjson",
                request=_process_request(
                    command=command,
                    name=name,
                    working_dir=working_dir,
                    env=env,
                    timeout=timeout,
                    wait_for_completion=True,
                    keep_alive=keep_alive,
                    max_restarts=max_restarts,
                    restart_on_failure=restart_on_failure,
                    stdin=stdin,
                    wait_for_ports=wait_for_ports,
                ),
            )
        return SandboxStream(response, _exec_events)

    def get(self, *, identifier: str) -> SandboxProcessInfo:
        """Get a process.

        Args:
            identifier: PID or name of the process.
        """
        process = self._context.call_read(
            lambda api: api.get_process_identifier(identifier=identifier)
        )
        return process_info_from_api(process)

    # builtins.list, since the method shadows it in the class body.
    def list(self) -> builtins.list[SandboxProcessInfo]:
        """List the sandbox's processes, running and finished."""
        processes = self._context.call_read(lambda api: api.get_process())
        return [process_info_from_api(process) for process in processes.root]

    def stop(self, *, identifier: str) -> None:
        """Request that a process exit gracefully, without waiting.

        Use :meth:`wait` for it to end, with status ``stopped``.

        Args:
            identifier: PID or name of the process.
        """
        with self._context.api() as api:
            api.delete_process(identifier=identifier)

    def kill(self, *, identifier: str) -> None:
        """Request that a process exit forcefully, without waiting.

        Use :meth:`wait` for it to end, with status ``killed``.

        Args:
            identifier: PID or name of the process.
        """
        with self._context.api() as api:
            api.delete_process_kill(identifier=identifier)

    def logs(self, *, identifier: str) -> SandboxProcessLogs:
        """Get a process's output so far.

        Args:
            identifier: PID or name of the process.
        """
        logs = self._context.call_read(
            lambda api: api.get_process_logs(identifier=identifier)
        )
        return SandboxProcessLogs(
            stdout=logs.stdout, stderr=logs.stderr, logs=logs.logs
        )

    def stream_logs(self, *, identifier: str) -> SandboxStream[SandboxProcessLogLine]:
        """Stream a process's output line by line as it arrives.

        Starts from the process's first output and ends when the process
        exits. Works for any process, including one started elsewhere. A
        partial line, such as a prompt, is yielded once its newline arrives
        or the stream ends. Use :meth:`exec_stream` for output as it is
        written. Closing the stream early stops only the stream, not the
        process.

        Args:
            identifier: PID or name of the process.
        """
        with self._context.api() as api:
            response = api.get_process_logs_stream(identifier=identifier)
        return SandboxStream(response, _log_lines)

    def wait(
        self,
        *,
        identifier: str,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_WAIT_POLL_INTERVAL,
    ) -> SandboxProcessInfo:
        """Wait for a process to finish, returning it whether it succeeded or not.

        Checks the process at each poll interval, riding out errors the
        sandbox may answer differently on the next check.

        Args:
            identifier: PID or name of the process.
            timeout: How long to wait. ``None`` waits with no limit.
            poll_interval: How long between checks.

        Raises:
            SandboxProcessWaitTimeoutError: The process was still running
                when the wait timed out. The process keeps running.
        """
        # A check already in flight cannot be interrupted, so the wait can
        # run over by up to one check.
        deadline = (
            None if timeout is None else time.monotonic() + timeout.total_seconds()
        )
        last_error: Exception | None = None
        while True:
            try:
                with self._context.api() as api:
                    process = api.get_process_identifier(identifier=identifier)
            except Exception as err:
                if not _is_retryable_wait_error(err):
                    raise
                last_error = err
            else:
                last_error = None
                info = _finished_process(identifier, process)
                if info is not None:
                    return info
            delay = poll_interval.total_seconds()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    time.sleep(max(remaining, 0))
                    raise SandboxProcessWaitTimeoutError(
                        identifier=identifier
                    ) from last_error
            time.sleep(delay)

    def write_stdin(self, *, identifier: str, data: str | bytes) -> None:
        """Write to the standard input of a process started with ``stdin=True``.

        Args:
            identifier: PID or name of the process.
            data: Bytes to write, sent as is. A string is written as UTF-8.
                Include any trailing newline the process expects.
        """
        # Not retried, since a repeat could write the bytes twice.
        with self._context.api() as api:
            api.post_process_stdin(identifier=identifier, content=data)

    def close_stdin(self, *, identifier: str) -> None:
        """Close a process's standard input, so it reads end of file.

        Args:
            identifier: PID or name of the process.
        """
        self._context.call_read(
            lambda api: api.delete_process_stdin(identifier=identifier)
        )


class AsyncSandboxProcess:
    """Async form of :class:`SandboxProcess`."""

    def __init__(self, context: AsyncSandboxContext) -> None:
        """Internal. Obtained from :attr:`AsyncSandbox.process`.

        :meta private:
        """
        self._context = context

    async def exec(
        self,
        *,
        command: str,
        name: str | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: timedelta | None = None,
        wait_for_completion: bool | None = None,
        keep_alive: bool | None = None,
        max_restarts: int | None = None,
        restart_on_failure: bool | None = None,
        stdin: bool | None = None,
        wait_for_ports: Sequence[int] | None = None,
    ) -> SandboxProcessInfo:
        """Start a command. As :meth:`SandboxProcess.exec`."""
        with self._context.api() as api:
            process = await api.post_process(
                request=_process_request(
                    command=command,
                    name=name,
                    working_dir=working_dir,
                    env=env,
                    timeout=timeout,
                    wait_for_completion=wait_for_completion,
                    keep_alive=keep_alive,
                    max_restarts=max_restarts,
                    restart_on_failure=restart_on_failure,
                    stdin=stdin,
                    wait_for_ports=wait_for_ports,
                )
            )
        return process_info_from_api(process)

    async def exec_stream(
        self,
        *,
        command: str,
        name: str | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: timedelta | None = None,
        keep_alive: bool | None = None,
        max_restarts: int | None = None,
        restart_on_failure: bool | None = None,
        stdin: bool | None = None,
        wait_for_ports: Sequence[int] | None = None,
    ) -> AsyncSandboxStream[SandboxProcessExecEvent]:
        """Run a command, streaming its output. As :meth:`SandboxProcess.exec_stream`."""
        with self._context.api() as api:
            response = await api.post_process_raw(
                accept="application/x-ndjson",
                request=_process_request(
                    command=command,
                    name=name,
                    working_dir=working_dir,
                    env=env,
                    timeout=timeout,
                    wait_for_completion=True,
                    keep_alive=keep_alive,
                    max_restarts=max_restarts,
                    restart_on_failure=restart_on_failure,
                    stdin=stdin,
                    wait_for_ports=wait_for_ports,
                ),
            )
        return AsyncSandboxStream(response, _exec_events_async)

    async def get(self, *, identifier: str) -> SandboxProcessInfo:
        """Get a process. As :meth:`SandboxProcess.get`."""
        process = await self._context.call_read(
            lambda api: api.get_process_identifier(identifier=identifier)
        )
        return process_info_from_api(process)

    # builtins.list, since the method shadows it in the class body.
    async def list(self) -> builtins.list[SandboxProcessInfo]:
        """List the sandbox's processes. As :meth:`SandboxProcess.list`."""
        processes = await self._context.call_read(lambda api: api.get_process())
        return [process_info_from_api(process) for process in processes.root]

    async def stop(self, *, identifier: str) -> None:
        """Request that a process exit gracefully. As :meth:`SandboxProcess.stop`."""
        with self._context.api() as api:
            await api.delete_process(identifier=identifier)

    async def kill(self, *, identifier: str) -> None:
        """Request that a process exit forcefully. As :meth:`SandboxProcess.kill`."""
        with self._context.api() as api:
            await api.delete_process_kill(identifier=identifier)

    async def logs(self, *, identifier: str) -> SandboxProcessLogs:
        """Get a process's output so far. As :meth:`SandboxProcess.logs`."""
        logs = await self._context.call_read(
            lambda api: api.get_process_logs(identifier=identifier)
        )
        return SandboxProcessLogs(
            stdout=logs.stdout, stderr=logs.stderr, logs=logs.logs
        )

    async def stream_logs(
        self, *, identifier: str
    ) -> AsyncSandboxStream[SandboxProcessLogLine]:
        """Stream a process's output. As :meth:`SandboxProcess.stream_logs`."""
        with self._context.api() as api:
            response = await api.get_process_logs_stream(identifier=identifier)
        return AsyncSandboxStream(response, _log_lines_async)

    async def wait(
        self,
        *,
        identifier: str,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_WAIT_POLL_INTERVAL,
    ) -> SandboxProcessInfo:
        """Wait for a process to finish. As :meth:`SandboxProcess.wait`."""
        last_error: Exception | None = None
        scope = asyncio.timeout(None if timeout is None else timeout.total_seconds())
        try:
            async with scope:
                while True:
                    try:
                        with self._context.api() as api:
                            process = await api.get_process_identifier(
                                identifier=identifier
                            )
                    except Exception as err:
                        if not _is_retryable_wait_error(err):
                            raise
                        last_error = err
                    else:
                        last_error = None
                        info = _finished_process(identifier, process)
                        if info is not None:
                            return info
                    await asyncio.sleep(poll_interval.total_seconds())
        except TimeoutError:
            # Only this wait's own timeout, not a caller's or one from a
            # check, becomes the wait timeout error.
            if not scope.expired():
                raise
            raise SandboxProcessWaitTimeoutError(identifier=identifier) from last_error

    async def write_stdin(self, *, identifier: str, data: str | bytes) -> None:
        """Write to a process's standard input. As :meth:`SandboxProcess.write_stdin`."""
        with self._context.api() as api:
            await api.post_process_stdin(identifier=identifier, content=data)

    async def close_stdin(self, *, identifier: str) -> None:
        """Close a process's standard input. As :meth:`SandboxProcess.close_stdin`."""
        await self._context.call_read(
            lambda api: api.delete_process_stdin(identifier=identifier)
        )


def process_info_from_api(
    process: baseten.client.sandboxapi.ProcessResponse,
) -> SandboxProcessInfo:
    """Convert a process from the execution API."""
    return SandboxProcessInfo(
        pid=process.pid,
        name=process.name,
        command=process.command,
        status=process.status,
        exit_code=process.exitCode,
        stdout=process.stdout,
        stderr=process.stderr,
        logs=process.logs,
        working_dir=process.workingDir,
        started_at=parse_timestamp(process.startedAt, "process startedAt"),
        # Empty while the process runs.
        completed_at=parse_timestamp(process.completedAt, "process completedAt")
        if process.completedAt
        else None,
        keep_alive=process.keepAlive,
        max_restarts=process.maxRestarts,
        restart_count=process.restartCount,
        restart_on_failure=process.restartOnFailure,
        stdin=process.stdin,
    )


def _process_request(
    *,
    command: str,
    name: str | None,
    working_dir: str | None,
    env: Mapping[str, str] | None,
    timeout: timedelta | None,
    wait_for_completion: bool | None,
    keep_alive: bool | None,
    max_restarts: int | None,
    restart_on_failure: bool | None,
    stdin: bool | None,
    wait_for_ports: Sequence[int] | None,
) -> baseten.client.sandboxapi.ProcessRequest:
    """Build the execution API request to run a command."""
    return baseten.client.sandboxapi.ProcessRequest(
        command=command,
        **set_fields(
            name=name,
            workingDir=working_dir,
            env=None if env is None else dict(env),
            # Whole seconds, rounded up.
            timeout=None if timeout is None else -(-timeout // timedelta(seconds=1)),
            waitForCompletion=wait_for_completion,
            keepAlive=keep_alive,
            maxRestarts=max_restarts,
            restartOnFailure=restart_on_failure,
            stdin=stdin,
            waitForPorts=None if wait_for_ports is None else [*wait_for_ports],
        ),
    )


def _finished_process(
    identifier: str, process: baseten.client.sandboxapi.ProcessResponse
) -> SandboxProcessInfo | None:
    """Return a process a wait is done with, or ``None`` while it runs."""
    if process.status in _TERMINAL_STATUSES:
        return process_info_from_api(process)
    # Unlike elsewhere, an unknown status raises rather than being waited on,
    # keeping existing behavior.
    if process.status != "running":
        raise SandboxError(f"process {identifier} has unknown status {process.status}")
    return None


def _is_retryable_wait_error(err: Exception) -> bool:
    """Report whether a wait should ride out an error from a check."""
    if isinstance(err, SandboxAPIError):
        return err.status in _WAIT_RETRY_STATUSES
    return is_transient_reset_error(err)


def _exec_event(line: str) -> SandboxProcessExecEvent | None:
    """Parse one line of a run's stream, or ``None`` for a line to skip."""
    if line == "":
        return None
    record = json.loads(line)
    if not isinstance(record, dict):
        return None
    kind = record.get("type")
    data = record.get("data")
    if kind == "stdout" or kind == "stderr":
        return SandboxProcessExecEventOutput(
            stream=kind, text="" if data is None else str(data)
        )
    if kind == "result":
        process = baseten.client.sandboxapi.ProcessResponse.model_validate_json(
            str(data)
        )
        return SandboxProcessExecEventExit(process=process_info_from_api(process))
    if kind == "error":
        # The response status is already sent, so a failure after streaming
        # starts arrives as a record that ends the stream.
        raise SandboxError(f"process stream failed: {'' if data is None else data}")
    # keepalive and any other record type are skipped, so new ones don't break
    # streaming.
    return None


def _exec_events(
    lines: Iterator[str],
) -> Generator[SandboxProcessExecEvent, None, None]:
    """Parse a run's stream into events, ending with its exit."""
    for line in lines:
        event = _exec_event(line)
        if event is None:
            continue
        yield event
        if isinstance(event, SandboxProcessExecEventExit):
            return
    raise SandboxError("the process stream ended before reporting the process's exit")


async def _exec_events_async(
    lines: AsyncIterator[str],
) -> AsyncGenerator[SandboxProcessExecEvent, None]:
    """Async form of :func:`_exec_events`."""
    async for line in lines:
        event = _exec_event(line)
        if event is None:
            continue
        yield event
        if isinstance(event, SandboxProcessExecEventExit):
            return
    raise SandboxError("the process stream ended before reporting the process's exit")


def _log_line(line: str) -> SandboxProcessLogLine | None:
    """Parse one line of a log stream, or ``None`` for a line to skip."""
    if line.startswith("[keepalive]"):
        return None
    if line.startswith("stdout:"):
        return SandboxProcessLogLine(stream="stdout", text=line[len("stdout:") :])
    if line.startswith("stderr:"):
        return SandboxProcessLogLine(stream="stderr", text=line[len("stderr:") :])
    return SandboxProcessLogLine(stream=None, text=line)


def _log_lines(lines: Iterator[str]) -> Generator[SandboxProcessLogLine, None, None]:
    """Parse a log stream into lines."""
    for line in lines:
        log_line = _log_line(line)
        if log_line is not None:
            yield log_line


async def _log_lines_async(
    lines: AsyncIterator[str],
) -> AsyncGenerator[SandboxProcessLogLine, None]:
    """Async form of :func:`_log_lines`."""
    async for line in lines:
        log_line = _log_line(line)
        if log_line is not None:
            yield log_line

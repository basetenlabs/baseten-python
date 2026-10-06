"""End-to-end tests of baseten.sandbox against a live Baseten environment.

Requires the following environment variables:
  BASETEN_E2E_TEST_API_KEY  — API key for authentication
  BASETEN_E2E_TEST_DOMAIN   — Domain (e.g. baseten.co)

Optional:
  BASETEN_E2E_TEST_DEBUG=1      — Print every request and response, credentials
                                  masked
  BASETEN_E2E_TEST_KEEP_SANDBOX — Leave the shared sandbox in place after the run

Skipped when BASETEN_E2E_TEST_API_KEY is empty or unset. If the API key is set
but DOMAIN is missing, the tests error.

Every test shares one sandbox, created once per run and always deleted. Each
test runs on both the sync and the async client. One image is pushed per run,
except on Python 3.11, and a sandbox created from it; both are always deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pydantic
import pytest
import pytest_asyncio

from baseten.sandbox import (
    AsyncSandbox,
    AsyncSandboxClient,
    ImageBuilder,
    Sandbox,
    SandboxAPIError,
    SandboxClient,
    SandboxEnvValue,
    SandboxError,
    SandboxExpirationPolicyIdle,
    SandboxFileSystemCopyError,
    SandboxFileSystemWatchEvent,
    SandboxInfo,
    SandboxLifecycle,
    SandboxProcessExecEvent,
    SandboxProcessExecEventExit,
    SandboxProcessExecEventOutput,
    SandboxProcessWaitTimeoutError,
)

API_KEY = os.environ.get("BASETEN_E2E_TEST_API_KEY", "")
DOMAIN = os.environ.get("BASETEN_E2E_TEST_DOMAIN", "")
DEBUG = os.environ.get("BASETEN_E2E_TEST_DEBUG") == "1"
KEEP_SANDBOX = bool(os.environ.get("BASETEN_E2E_TEST_KEEP_SANDBOX"))

# Labels on every sandbox the suite creates.
_LABELS = {"created_by": "e2e"}

# Env on the shared sandbox, by name.
_SHARED_ENVS = {
    "E2E_PLAIN": SandboxEnvValue(value="plain-value", secret=False),
    "E2E_SECRET": SandboxEnvValue(value="secret-value", secret=True),
    # Left to the server's default, which is secret.
    "E2E_DEFAULT": SandboxEnvValue(value="default-value"),
}

# Allowance between this machine's clock and the server's.
_CLOCK_SKEW = timedelta(minutes=5)

# The SDK's own timeouts, for the debug clients that replace its HTTP client.
_DEBUG_TIMEOUT = httpx.Timeout(5.0, connect=10.0, read=None)


@dataclass
class _Shared:
    """The sandbox every test shares."""

    name: str
    info: SandboxInfo
    """Its record as created."""
    started: datetime
    """When the run started creating it."""


@pytest.fixture(scope="session")
def shared() -> Iterator[_Shared]:
    _require_env()
    client = _client()
    name = _unique()
    started = datetime.now(UTC)
    # The name is known before creating, so even a partial create is removed.
    try:
        created = client.create(
            name=name,
            # TODO: Temporary, while other regions' exec planes reject our
            # tokens.
            region="us-was-1",
            labels=_LABELS,
            envs=_SHARED_ENVS,
        )
        # Create returns once the sandbox is up, so there is nothing to wait
        # for.
        assert created.info.status == "DEPLOYED"
    except BaseException:
        try:
            client.delete(name=name)
        except SandboxAPIError as err:
            # A create that never got through leaves nothing to delete. The
            # original error is raised either way.
            if err.status != 404:
                print(f"failed to delete sandbox {name} after setup failed: {err}")
        client.close()
        raise
    try:
        yield _Shared(name=name, info=created.info, started=started)
    finally:
        try:
            if KEEP_SANDBOX:
                print(f"BASETEN_E2E_TEST_KEEP_SANDBOX set; leaving sandbox {name}")
            else:
                deleted = client.delete(name=name)
                assert (deleted.name, deleted.status) == (name, "DELETING")
        finally:
            client.close()


# Each test runs on both clients: the sync one's calls go to a thread, so test
# bodies stay async for both.
@pytest_asyncio.fixture(params=["sync", "async"])
async def client(
    request: pytest.FixtureRequest,
) -> AsyncIterator[SandboxClient | AsyncSandboxClient]:
    if request.param == "async":
        async with _async_client() as async_client:
            yield async_client
    else:
        with _client() as sync_client:
            yield sync_client


@pytest.mark.asyncio
async def test_gets_record(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    if isinstance(client, SandboxClient):
        info = await asyncio.to_thread(client.get_info, name=shared.name)
    else:
        info = await client.get_info(name=shared.name)
    assert info.name == shared.name
    assert info.status == "DEPLOYED"
    assert info.url.startswith("https://")
    assert info.labels.items() >= _LABELS.items()
    assert shared.started - _CLOCK_SKEW <= info.created_at
    assert info.created_at <= datetime.now(UTC) + _CLOCK_SKEW
    # Only an env set as not secret comes back unmasked, and one left unset is
    # secret.
    assert info.envs == {
        "E2E_PLAIN": SandboxEnvValue(value="plain-value", secret=False),
        "E2E_SECRET": SandboxEnvValue(value="****", secret=True),
        "E2E_DEFAULT": SandboxEnvValue(value="****", secret=True),
    }


@pytest.mark.asyncio
async def test_gets_record_with_secrets_revealed(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    # The e2e key is a workspace admin, so secrets are revealed.
    if isinstance(client, SandboxClient):
        info = await asyncio.to_thread(
            client.get_info, name=shared.name, show_secrets=True
        )
    else:
        info = await client.get_info(name=shared.name, show_secrets=True)
    assert info.envs == {
        "E2E_PLAIN": SandboxEnvValue(value="plain-value", secret=False),
        "E2E_SECRET": SandboxEnvValue(value="secret-value", secret=True),
        "E2E_DEFAULT": SandboxEnvValue(value="default-value", secret=True),
    }


@pytest.mark.asyncio
async def test_gets_sandbox_by_name_record_and_url(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    # Reaching the sandbox from its URL checks the minted token works there too.
    if isinstance(client, SandboxClient):
        info = await asyncio.to_thread(client.get_info, name=shared.name)
        by_name = await asyncio.to_thread(client.get, name=shared.name)
        from_info = await asyncio.to_thread(client.get, info=info)
        from_url = client.sandbox_from_url(url=info.url)
        processes = await asyncio.to_thread(from_url.process.list)
    else:
        info = await client.get_info(name=shared.name)
        by_name = await client.get(name=shared.name)
        from_info = await client.get(info=info)
        from_url = client.sandbox_from_url(url=info.url)
        processes = await from_url.process.list()
    for sandbox in (by_name, from_info):
        assert (sandbox.name, sandbox.url) == (shared.name, info.url)
    assert from_url.name == ""
    assert isinstance(processes, list)


@pytest.mark.asyncio
async def test_lists(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    if isinstance(client, SandboxClient):
        infos = await asyncio.to_thread(lambda: list(client.list(query=shared.name)))
    else:
        infos = [info async for info in client.list(query=shared.name)]
    assert shared.name in [info.name for info in infos]


@pytest.mark.asyncio
async def test_create_if_not_exists_returns_existing(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    if isinstance(client, SandboxClient):
        before = await asyncio.to_thread(client.get_info, name=shared.name)
        created = await asyncio.to_thread(
            client.create,
            name=shared.name,
            create_if_not_exists=True,
            labels={"other": "1"},
        )
    else:
        before = await client.get_info(name=shared.name)
        created = await client.create(
            name=shared.name, create_if_not_exists=True, labels={"other": "1"}
        )
    assert (created.name, created.url) == (shared.name, before.url)
    assert created.info.created_at == before.created_at
    # The existing configuration is kept, not replaced by the request's.
    assert created.info.labels == before.labels


# Changes only fields no other test on the shared sandbox depends on.
@pytest.mark.asyncio
async def test_update_replaces_labels(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    labels = {**_LABELS, "e2e_extra": "1"}
    lifecycle = SandboxLifecycle(
        expiration_policies=[SandboxExpirationPolicyIdle(after=timedelta(hours=1))],
        terminated_retention=timedelta(minutes=10),
    )
    if isinstance(client, SandboxClient):
        updated = await asyncio.to_thread(
            client.update, name=shared.name, lifecycle=lifecycle, labels=labels
        )
        fetched = await asyncio.to_thread(client.get_info, name=shared.name)
        replaced = await asyncio.to_thread(
            client.update, name=shared.name, labels=_LABELS
        )
    else:
        updated = await client.update(
            name=shared.name, lifecycle=lifecycle, labels=labels
        )
        fetched = await client.get_info(name=shared.name)
        replaced = await client.update(name=shared.name, labels=_LABELS)
    for info in (updated, fetched):
        assert info.lifecycle == SandboxLifecycle(
            expiration_policies=[
                SandboxExpirationPolicyIdle(after=timedelta(hours=1), action="DELETE")
            ],
            terminated_retention=timedelta(minutes=10),
        )
        assert info.labels == labels
    assert replaced.labels == _LABELS


@pytest.mark.asyncio
async def test_update_with_nothing_fails(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    with pytest.raises(SandboxAPIError) as info:
        if isinstance(client, SandboxClient):
            await asyncio.to_thread(client.update, name=shared.name)
        else:
            await client.update(name=shared.name)
    assert info.value.status == 400


@pytest.mark.asyncio
async def test_get_missing_fails(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> None:
    missing = f"{shared.name}-missing"
    with pytest.raises(SandboxAPIError) as info:
        if isinstance(client, SandboxClient):
            await asyncio.to_thread(client.get_info, name=missing)
        else:
            await client.get_info(name=missing)
    assert info.value.status == 404
    assert info.value.code is not None


@pytest_asyncio.fixture
async def sandbox(
    client: SandboxClient | AsyncSandboxClient, shared: _Shared
) -> Sandbox | AsyncSandbox:
    if isinstance(client, SandboxClient):
        return await asyncio.to_thread(client.get, name=shared.name)
    return await client.get(name=shared.name)


@pytest.mark.asyncio
async def test_lists_library_images(
    client: SandboxClient | AsyncSandboxClient,
) -> None:
    _require_env()
    if isinstance(client, SandboxClient):
        images = await asyncio.to_thread(client.images.list_library)
    else:
        images = await client.images.list_library()
    assert all(image.image for image in images)
    base = [image for image in images if image.name == "base-image"]
    assert [image.image for image in base] == ["baseten/base-image:latest"]


@pytest.mark.asyncio
async def test_process_runs_to_completion(sandbox: Sandbox | AsyncSandbox) -> None:
    started = datetime.now(UTC)
    command = "sh -c 'echo out; echo err >&2; exit 3'"
    if isinstance(sandbox, Sandbox):
        process = await asyncio.to_thread(
            sandbox.process.exec, command=command, wait_for_completion=True
        )
    else:
        process = await sandbox.process.exec(command=command, wait_for_completion=True)
    assert process.exit_code == 3
    assert (process.stdout.strip(), process.stderr.strip()) == ("out", "err")
    _assert_within_run(started, process.started_at)
    assert process.completed_at is not None
    _assert_within_run(started, process.completed_at)


@pytest.mark.asyncio
async def test_process_working_dir_and_env(sandbox: Sandbox | AsyncSandbox) -> None:
    command = "sh -c 'pwd; echo $E2E_PLAIN; echo $E2E_SECRET; echo $E2E_EXEC'"
    options: dict[str, Any] = {
        "command": command,
        "working_dir": "/tmp",
        "env": {"E2E_EXEC": "exec-value"},
        "wait_for_completion": True,
    }
    if isinstance(sandbox, Sandbox):
        process = await asyncio.to_thread(lambda: sandbox.process.exec(**options))
    else:
        process = await sandbox.process.exec(**options)
    # The sandbox's envs, secret ones too, reach its processes.
    assert process.stdout.split() == [
        "/tmp",
        "plain-value",
        "secret-value",
        "exec-value",
    ]


@pytest.mark.asyncio
async def test_process_gets_lists_and_waits_for_background_process(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    name = _unique()
    command = "sh -c 'sleep 1; echo done'"
    try:
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(sandbox.process.exec, command=command, name=name)
            got = await asyncio.to_thread(sandbox.process.get, identifier=name)
            processes = await asyncio.to_thread(sandbox.process.list)
            finished = await asyncio.to_thread(sandbox.process.wait, identifier=name)
            logs = await asyncio.to_thread(sandbox.process.logs, identifier=got.pid)
        else:
            await sandbox.process.exec(command=command, name=name)
            got = await sandbox.process.get(identifier=name)
            processes = await sandbox.process.list()
            finished = await sandbox.process.wait(identifier=name)
            logs = await sandbox.process.logs(identifier=got.pid)
    finally:
        await _kill_quietly(sandbox, name)
    assert (got.name, got.status, got.completed_at) == (name, "running", None)
    assert name in [process.name for process in processes]
    assert (finished.status, finished.exit_code) == ("completed", 0)
    assert logs.stdout.strip() == "done"


@pytest.mark.asyncio
async def test_process_stops_and_kills(sandbox: Sandbox | AsyncSandbox) -> None:
    stopped, killed = _unique(), _unique()
    try:
        if isinstance(sandbox, Sandbox):
            for name in (stopped, killed):
                await asyncio.to_thread(
                    sandbox.process.exec, command="sleep 300", name=name
                )
            await asyncio.to_thread(sandbox.process.stop, identifier=stopped)
            await asyncio.to_thread(sandbox.process.kill, identifier=killed)
            statuses = [
                (await asyncio.to_thread(sandbox.process.wait, identifier=name)).status
                for name in (stopped, killed)
            ]
        else:
            for name in (stopped, killed):
                await sandbox.process.exec(command="sleep 300", name=name)
            await sandbox.process.stop(identifier=stopped)
            await sandbox.process.kill(identifier=killed)
            statuses = [
                (await sandbox.process.wait(identifier=name)).status
                for name in (stopped, killed)
            ]
    finally:
        for name in (stopped, killed):
            await _kill_quietly(sandbox, name)
    assert statuses == ["stopped", "killed"]


@pytest.mark.asyncio
async def test_process_writes_and_closes_stdin(sandbox: Sandbox | AsyncSandbox) -> None:
    name = _unique()
    try:
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(
                sandbox.process.exec, command="cat", name=name, stdin=True
            )
            await asyncio.to_thread(
                sandbox.process.write_stdin, identifier=name, data="hello\n"
            )
            await asyncio.to_thread(
                sandbox.process.write_stdin, identifier=name, data=b"more\n"
            )
            await asyncio.to_thread(sandbox.process.close_stdin, identifier=name)
            finished = await asyncio.to_thread(sandbox.process.wait, identifier=name)
        else:
            await sandbox.process.exec(command="cat", name=name, stdin=True)
            await sandbox.process.write_stdin(identifier=name, data="hello\n")
            await sandbox.process.write_stdin(identifier=name, data=b"more\n")
            await sandbox.process.close_stdin(identifier=name)
            finished = await sandbox.process.wait(identifier=name)
    finally:
        await _kill_quietly(sandbox, name)
    assert finished.stdin
    assert (finished.status, finished.stdout) == ("completed", "hello\nmore\n")


@pytest.mark.asyncio
async def test_process_write_stdin_without_stdin_fails(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    name = _unique()
    try:
        with pytest.raises(SandboxAPIError) as raised:
            if isinstance(sandbox, Sandbox):
                await asyncio.to_thread(
                    sandbox.process.exec, command="sleep 300", name=name
                )
                await asyncio.to_thread(
                    sandbox.process.write_stdin, identifier=name, data="hello\n"
                )
            else:
                await sandbox.process.exec(command="sleep 300", name=name)
                await sandbox.process.write_stdin(identifier=name, data="hello\n")
    finally:
        await _kill_quietly(sandbox, name)
    assert raised.value.status == 409


@pytest.mark.asyncio
async def test_process_streams_logs_of_running_and_finished_processes(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    running, finished = _unique(), _unique()
    try:
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(
                sandbox.process.exec,
                command="sh -c 'sleep 1; echo a; echo b >&2; echo c'",
                name=running,
            )
            running_lines = await asyncio.to_thread(
                lambda: list(sandbox.process.stream_logs(identifier=running))
            )
            await asyncio.to_thread(
                sandbox.process.exec, command="sh -c 'echo a; echo b'", name=finished
            )
            await asyncio.to_thread(sandbox.process.wait, identifier=finished)
            finished_lines = await asyncio.to_thread(
                lambda: list(sandbox.process.stream_logs(identifier=finished))
            )
        else:
            await sandbox.process.exec(
                command="sh -c 'sleep 1; echo a; echo b >&2; echo c'", name=running
            )
            stream = await sandbox.process.stream_logs(identifier=running)
            running_lines = [line async for line in stream]
            await sandbox.process.exec(command="sh -c 'echo a; echo b'", name=finished)
            await sandbox.process.wait(identifier=finished)
            stream = await sandbox.process.stream_logs(identifier=finished)
            finished_lines = [line async for line in stream]
    finally:
        for name in (running, finished):
            await _kill_quietly(sandbox, name)
    # Order holds within each stream, not between them.
    by_stream = {
        stream: [line.text for line in running_lines if line.stream == stream]
        for stream in ("stdout", "stderr")
    }
    assert by_stream == {"stdout": ["a", "c"], "stderr": ["b"]}
    assert len(running_lines) == 3
    # A finished process's logs replay from its start.
    assert [(line.stream, line.text) for line in finished_lines] == [
        ("stdout", "a"),
        ("stdout", "b"),
    ]


@pytest.mark.asyncio
async def test_process_wait_timeout_leaves_process_running(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    name = _unique()
    try:
        with pytest.raises(SandboxProcessWaitTimeoutError) as raised:
            if isinstance(sandbox, Sandbox):
                await asyncio.to_thread(
                    sandbox.process.exec, command="sleep 300", name=name
                )
                await asyncio.to_thread(
                    sandbox.process.wait, identifier=name, timeout=timedelta(seconds=1)
                )
            else:
                await sandbox.process.exec(command="sleep 300", name=name)
                await sandbox.process.wait(
                    identifier=name, timeout=timedelta(seconds=1)
                )
        if isinstance(sandbox, Sandbox):
            process = await asyncio.to_thread(sandbox.process.get, identifier=name)
        else:
            process = await sandbox.process.get(identifier=name)
    finally:
        await _kill_quietly(sandbox, name)
    assert raised.value.identifier == name
    assert process.status == "running"


@pytest.mark.asyncio
async def test_process_exec_stream_output_then_exit(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    command = "sh -c 'printf \"a\\n\\nb\\n\"; echo e >&2; exit 2'"
    if isinstance(sandbox, Sandbox):

        def run() -> list[SandboxProcessExecEvent]:
            with sandbox.process.exec_stream(command=command) as stream:
                return list(stream)

        events = await asyncio.to_thread(run)
    else:
        async with await sandbox.process.exec_stream(command=command) as stream:
            events = [event async for event in stream]
    *outputs, exit_event = events
    assert isinstance(exit_event, SandboxProcessExecEventExit)
    assert exit_event.process.exit_code == 2
    # Chunks follow the process's writes, so only what they join to is fixed.
    joined = {"stdout": "", "stderr": ""}
    for output in outputs:
        assert isinstance(output, SandboxProcessExecEventOutput)
        joined[output.stream] += output.text
    assert joined == {"stdout": "a\n\nb\n", "stderr": "e\n"}


@pytest.mark.asyncio
async def test_process_exec_stream_prompt_before_newline(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    name = _unique()
    command = 'sh -c \'printf "Continue? "; read answer; echo "got $answer"\''
    stdout = ""
    exit_code: int | None = None
    try:
        if isinstance(sandbox, Sandbox):

            def run() -> tuple[str, int | None]:
                text = ""
                with sandbox.process.exec_stream(
                    command=command, name=name, stdin=True
                ) as stream:
                    for event in stream:
                        if isinstance(event, SandboxProcessExecEventExit):
                            return text, event.process.exit_code
                        if event.stream != "stdout":
                            continue
                        text += event.text
                        # The process blocks on the read, so this only happens
                        # if the prompt arrived without its line finished.
                        if text == "Continue? ":
                            sandbox.process.write_stdin(identifier=name, data="yes\n")
                return text, None

            stdout, exit_code = await asyncio.to_thread(run)
        else:
            async with await sandbox.process.exec_stream(
                command=command, name=name, stdin=True
            ) as stream:
                async for event in stream:
                    if isinstance(event, SandboxProcessExecEventExit):
                        exit_code = event.process.exit_code
                        break
                    if event.stream != "stdout":
                        continue
                    stdout += event.text
                    if stdout == "Continue? ":
                        await sandbox.process.write_stdin(identifier=name, data="yes\n")
    finally:
        await _kill_quietly(sandbox, name)
    assert (stdout, exit_code) == ("Continue? got yes\n", 0)


# The sandbox sends output as JSON strings, replacing invalid UTF-8 with U+FFFD
# before it reaches the SDK.
@pytest.mark.asyncio
async def test_process_non_utf8_output_replaced(
    sandbox: Sandbox | AsyncSandbox,
) -> None:
    command = "printf 'a\\377b'"
    if isinstance(sandbox, Sandbox):
        process = await asyncio.to_thread(
            sandbox.process.exec, command=command, wait_for_completion=True
        )
        logs = await asyncio.to_thread(sandbox.process.logs, identifier=process.pid)

        def stream_text() -> str:
            with sandbox.process.exec_stream(command=command) as stream:
                return "".join(
                    event.text
                    for event in stream
                    if isinstance(event, SandboxProcessExecEventOutput)
                )

        streamed = await asyncio.to_thread(stream_text)
    else:
        process = await sandbox.process.exec(command=command, wait_for_completion=True)
        logs = await sandbox.process.logs(identifier=process.pid)
        async with await sandbox.process.exec_stream(command=command) as stream:
            streamed = "".join(
                [
                    event.text
                    async for event in stream
                    if isinstance(event, SandboxProcessExecEventOutput)
                ]
            )
    assert process.stdout == logs.stdout == streamed == "a�b"


@pytest.mark.asyncio
async def test_process_unknown_fails(sandbox: Sandbox | AsyncSandbox) -> None:
    for call in ("logs", "get"):
        with pytest.raises(SandboxAPIError) as raised:
            if isinstance(sandbox, Sandbox):
                await asyncio.to_thread(
                    getattr(sandbox.process, call), identifier=_unique()
                )
            else:
                await getattr(sandbox.process, call)(identifier=_unique())
        assert raised.value.status == 404


@pytest_asyncio.fixture
async def fs_dir(sandbox: Sandbox | AsyncSandbox) -> str:
    # A directory of the test's own, so tests on the shared sandbox never see
    # each other's files.
    path = f"/tmp/e2e-fs/{_unique()}"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.mkdir, path=path)
    else:
        await sandbox.fs.mkdir(path=path)
    return path


@pytest.mark.asyncio
async def test_fs_writes_and_reads_including_missing_directories_and_relative(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    relative = f"{_unique()}.txt"
    paths = {
        f"{fs_dir}/hello.txt": "hello",
        f"{fs_dir}/a/b/c.txt": "nested",
        # Resolved against the sandbox's working directory.
        relative: "relative",
    }
    try:
        if isinstance(sandbox, Sandbox):
            for path, content in paths.items():
                await asyncio.to_thread(sandbox.fs.write, path=path, content=content)
            read = {
                path: await asyncio.to_thread(sandbox.fs.read, path=path)
                for path in paths
            }
        else:
            for path, content in paths.items():
                await sandbox.fs.write(path=path, content=content)
            read = {path: await sandbox.fs.read(path=path) for path in paths}
    finally:
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(sandbox.fs.remove, path=relative)
        else:
            await sandbox.fs.remove(path=relative)
    assert read == paths


@pytest.mark.asyncio
async def test_fs_read_missing_fails(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    with pytest.raises(SandboxAPIError) as raised:
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(sandbox.fs.read, path=f"{fs_dir}/missing.txt")
        else:
            await sandbox.fs.read(path=f"{fs_dir}/missing.txt")
    assert raised.value.status == 404


@pytest.mark.asyncio
async def test_fs_names_needing_escaping(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    path = f"{fs_dir}/a b%#?.txt"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.write, path=path, content="escaped")
        text = await asyncio.to_thread(sandbox.fs.read, path=path)
        await asyncio.to_thread(sandbox.fs.write_bytes, path=path, content=b"\x01\x02")
        data = await asyncio.to_thread(sandbox.fs.read_bytes, path=path)
        listing = await asyncio.to_thread(sandbox.fs.list, path=fs_dir)
    else:
        await sandbox.fs.write(path=path, content="escaped")
        text = await sandbox.fs.read(path=path)
        await sandbox.fs.write_bytes(path=path, content=b"\x01\x02")
        data = await sandbox.fs.read_bytes(path=path)
        listing = await sandbox.fs.list(path=fs_dir)
    assert (text, data) == ("escaped", b"\x01\x02")
    assert [file.name for file in listing.files] == ["a b%#?.txt"]


@pytest.mark.asyncio
async def test_fs_round_trips_every_byte_value(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    content = bytes(range(256))
    path = f"{fs_dir}/all.bin"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.write_bytes, path=path, content=content)
        read = await asyncio.to_thread(sandbox.fs.read_bytes, path=path)
    else:
        await sandbox.fs.write_bytes(path=path, content=content)
        read = await sandbox.fs.read_bytes(path=path)
    assert read == content


@pytest.mark.asyncio
async def test_fs_write_bytes_with_permissions_kept_on_overwrite(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    path = f"{fs_dir}/run.sh"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write_bytes,
            path=path,
            content=b"#!/bin/sh\necho ran\n",
            permissions="0755",
        )
        created_mode = await _mode(sandbox, path)
        ran = await asyncio.to_thread(
            sandbox.process.exec, command=path, wait_for_completion=True
        )
        await asyncio.to_thread(
            sandbox.fs.write_bytes,
            path=path,
            content=b"#!/bin/sh\necho again\n",
            permissions="0700",
        )
    else:
        await sandbox.fs.write_bytes(
            path=path, content=b"#!/bin/sh\necho ran\n", permissions="0755"
        )
        created_mode = await _mode(sandbox, path)
        ran = await sandbox.process.exec(command=path, wait_for_completion=True)
        await sandbox.fs.write_bytes(
            path=path, content=b"#!/bin/sh\necho again\n", permissions="0700"
        )
    assert created_mode == "755"
    assert ran.stdout.strip() == "ran"
    # An existing file keeps its mode.
    assert await _mode(sandbox, path) == "755"


@pytest.mark.asyncio
async def test_fs_writes_over_5mb_in_parts(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    # Three parts, the last one partial, in a pattern that does not line up
    # with the part size, so a part off by any number of bytes shows.
    content = bytes(index % 251 for index in range(12 * 1024 * 1024 + 17))
    # Two bytes each in UTF-8, with a line number so parts out of order show.
    text = "".join(f"{index} é\n" for index in range(1_000_000))
    binary_path, text_path = f"{fs_dir}/big.bin", f"{fs_dir}/big.txt"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write_bytes,
            path=binary_path,
            content=content,
            permissions="0755",
        )
        await asyncio.to_thread(sandbox.fs.write, path=text_path, content=text)
        read = await asyncio.to_thread(sandbox.fs.read_bytes, path=binary_path)
        read_text = await asyncio.to_thread(sandbox.fs.read, path=text_path)
    else:
        await sandbox.fs.write_bytes(
            path=binary_path, content=content, permissions="0755"
        )
        await sandbox.fs.write(path=text_path, content=text)
        read = await sandbox.fs.read_bytes(path=binary_path)
        read_text = await sandbox.fs.read(path=text_path)
    assert read == content
    assert read_text == text
    # Unlike a single upload's, a multipart upload's permissions apply.
    assert await _mode(sandbox, binary_path) == "755"


@pytest.mark.asyncio
async def test_fs_mkdir_with_permissions_making_parents_and_repeating(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.mkdir, path=f"{fs_dir}/private", permissions="0700"
        )
        for _ in range(2):
            await asyncio.to_thread(sandbox.fs.mkdir, path=f"{fs_dir}/x/y/z")
        listing = await asyncio.to_thread(sandbox.fs.list, path=f"{fs_dir}/x/y")
    else:
        await sandbox.fs.mkdir(path=f"{fs_dir}/private", permissions="0700")
        for _ in range(2):
            await sandbox.fs.mkdir(path=f"{fs_dir}/x/y/z")
        listing = await sandbox.fs.list(path=f"{fs_dir}/x/y")
    assert await _mode(sandbox, f"{fs_dir}/private") == "700"
    assert [subdirectory.name for subdirectory in listing.subdirectories] == ["z"]


@pytest.mark.asyncio
async def test_fs_read_directory_and_list_file_fail(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    file = f"{fs_dir}/f.txt"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.write, path=file, content="f")
        with pytest.raises(SandboxError, match="is a directory"):
            await asyncio.to_thread(sandbox.fs.read, path=fs_dir)
        with pytest.raises(SandboxError, match="is a directory"):
            await asyncio.to_thread(sandbox.fs.read_bytes, path=fs_dir)
        with pytest.raises(SandboxError, match="is a file"):
            await asyncio.to_thread(sandbox.fs.list, path=file)
    else:
        await sandbox.fs.write(path=file, content="f")
        with pytest.raises(SandboxError, match="is a directory"):
            await sandbox.fs.read(path=fs_dir)
        with pytest.raises(SandboxError, match="is a directory"):
            await sandbox.fs.read_bytes(path=fs_dir)
        with pytest.raises(SandboxError, match="is a file"):
            await sandbox.fs.list(path=file)


@pytest.mark.asyncio
async def test_fs_lists(sandbox: Sandbox | AsyncSandbox, fs_dir: str) -> None:
    before = datetime.now(UTC)
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write, path=f"{fs_dir}/a.txt", content="12345"
        )
        await asyncio.to_thread(sandbox.fs.mkdir, path=f"{fs_dir}/sub")
        listing = await asyncio.to_thread(sandbox.fs.list, path=fs_dir)
    else:
        await sandbox.fs.write(path=f"{fs_dir}/a.txt", content="12345")
        await sandbox.fs.mkdir(path=f"{fs_dir}/sub")
        listing = await sandbox.fs.list(path=fs_dir)
    assert listing.path == fs_dir
    assert [subdirectory.name for subdirectory in listing.subdirectories] == ["sub"]
    [file] = listing.files
    assert (file.name, file.path, file.size_bytes) == ("a.txt", f"{fs_dir}/a.txt", 5)
    _assert_within_run(before, file.last_modified)


@pytest.mark.asyncio
async def test_fs_removes_non_empty_directory_only_recursively(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    file, directory = f"{fs_dir}/f.txt", f"{fs_dir}/d"
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.write, path=file, content="f")
        await asyncio.to_thread(sandbox.fs.remove, path=file)
        with pytest.raises(SandboxAPIError) as removed_file:
            await asyncio.to_thread(sandbox.fs.read, path=file)
        await asyncio.to_thread(
            sandbox.fs.write, path=f"{directory}/g.txt", content="g"
        )
        with pytest.raises(SandboxAPIError):
            await asyncio.to_thread(sandbox.fs.remove, path=directory)
        await asyncio.to_thread(sandbox.fs.remove, path=directory, recursive=True)
        with pytest.raises(SandboxAPIError) as removed_directory:
            await asyncio.to_thread(sandbox.fs.list, path=directory)
    else:
        await sandbox.fs.write(path=file, content="f")
        await sandbox.fs.remove(path=file)
        with pytest.raises(SandboxAPIError) as removed_file:
            await sandbox.fs.read(path=file)
        await sandbox.fs.write(path=f"{directory}/g.txt", content="g")
        with pytest.raises(SandboxAPIError):
            await sandbox.fs.remove(path=directory)
        await sandbox.fs.remove(path=directory, recursive=True)
        with pytest.raises(SandboxAPIError) as removed_directory:
            await sandbox.fs.list(path=directory)
    assert removed_file.value.status == removed_directory.value.status == 404


@pytest.mark.asyncio
async def test_fs_finds_and_greps(sandbox: Sandbox | AsyncSandbox, fs_dir: str) -> None:
    files = {
        "a.ts": "a",
        "b.js": "b",
        "sub/c.ts": "c",
        "n.txt": "one\nfind Needle here\nthree\n",
    }
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(sandbox.fs.write_tree, path=fs_dir, files=files)
        found = await asyncio.to_thread(sandbox.fs.find, path=fs_dir, patterns=["*.ts"])
        directories = await asyncio.to_thread(
            sandbox.fs.find, path=fs_dir, type="directory"
        )
        grepped = await asyncio.to_thread(sandbox.fs.grep, path=fs_dir, query="needle")
        with_context = await asyncio.to_thread(
            sandbox.fs.grep, path=fs_dir, query="needle", context_lines=1
        )
        exact = await asyncio.to_thread(
            sandbox.fs.grep, path=fs_dir, query="needle", case_sensitive=True
        )
    else:
        await sandbox.fs.write_tree(path=fs_dir, files=files)
        found = await sandbox.fs.find(path=fs_dir, patterns=["*.ts"])
        directories = await sandbox.fs.find(path=fs_dir, type="directory")
        grepped = await sandbox.fs.grep(path=fs_dir, query="needle")
        with_context = await sandbox.fs.grep(
            path=fs_dir, query="needle", context_lines=1
        )
        exact = await sandbox.fs.grep(path=fs_dir, query="needle", case_sensitive=True)
    # Relative to the searched path, as observed from the server.
    assert sorted(match.path for match in found.matches) == ["a.ts", "sub/c.ts"]
    assert found.total == 2
    assert all(match.type == "directory" for match in directories.matches)
    assert "sub" in [match.path for match in directories.matches]
    [match] = grepped.matches
    assert (match.path, match.line, match.text) == ("n.txt", 2, "find Needle here")
    assert not match.context
    assert [match.context for match in with_context.matches] == [
        "one\nfind Needle here\nthree"
    ]
    assert exact.total == 0


@pytest.mark.asyncio
async def test_fs_copies_files_and_directories(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write, path=f"{fs_dir}/it's.txt", content="quoted"
        )
        await asyncio.to_thread(
            sandbox.fs.copy, source=f"{fs_dir}/it's.txt", destination=f"{fs_dir}/c.txt"
        )
        await asyncio.to_thread(
            sandbox.fs.write_tree,
            path=f"{fs_dir}/src",
            files={"a.txt": "a", "d/b.txt": "b"},
        )
        await asyncio.to_thread(
            sandbox.fs.copy, source=f"{fs_dir}/src", destination=f"{fs_dir}/dst"
        )
        copied = await asyncio.to_thread(sandbox.fs.read, path=f"{fs_dir}/c.txt")
        nested = await asyncio.to_thread(sandbox.fs.read, path=f"{fs_dir}/dst/d/b.txt")
        with pytest.raises(SandboxFileSystemCopyError) as raised:
            await asyncio.to_thread(
                sandbox.fs.copy, source=f"{fs_dir}/missing", destination=f"{fs_dir}/x"
            )
    else:
        await sandbox.fs.write(path=f"{fs_dir}/it's.txt", content="quoted")
        await sandbox.fs.copy(
            source=f"{fs_dir}/it's.txt", destination=f"{fs_dir}/c.txt"
        )
        await sandbox.fs.write_tree(
            path=f"{fs_dir}/src", files={"a.txt": "a", "d/b.txt": "b"}
        )
        await sandbox.fs.copy(source=f"{fs_dir}/src", destination=f"{fs_dir}/dst")
        copied = await sandbox.fs.read(path=f"{fs_dir}/c.txt")
        nested = await sandbox.fs.read(path=f"{fs_dir}/dst/d/b.txt")
        with pytest.raises(SandboxFileSystemCopyError) as raised:
            await sandbox.fs.copy(source=f"{fs_dir}/missing", destination=f"{fs_dir}/x")
    assert (copied, nested) == ("quoted", "b")
    assert "missing" in raised.value.process.stderr


@pytest.mark.asyncio
async def test_fs_write_tree_leaves_other_files(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    files = {"a.txt": "a", "d/e/b.txt": "b"}
    paths = ["a.txt", "d/e/b.txt", "keep.txt"]
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write, path=f"{fs_dir}/keep.txt", content="keep"
        )
        await asyncio.to_thread(sandbox.fs.write_tree, path=fs_dir, files=files)
        read = [
            await asyncio.to_thread(sandbox.fs.read, path=f"{fs_dir}/{path}")
            for path in paths
        ]
    else:
        await sandbox.fs.write(path=f"{fs_dir}/keep.txt", content="keep")
        await sandbox.fs.write_tree(path=fs_dir, files=files)
        read = [await sandbox.fs.read(path=f"{fs_dir}/{path}") for path in paths]
    assert read == ["a", "b", "keep"]


# The watch covers only the directory itself unless recursive.
@pytest.mark.asyncio
async def test_fs_watches_directory(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    event = await _watch_until_written(sandbox, f"{fs_dir}/top.txt", path=fs_dir)
    assert {"CREATE", "WRITE"} & set(event.ops)


@pytest.mark.asyncio
async def test_fs_watches_recursively(
    sandbox: Sandbox | AsyncSandbox, fs_dir: str
) -> None:
    # The subdirectory exists before the watch, so the event shows the watch
    # covers it.
    if isinstance(sandbox, Sandbox):
        await asyncio.to_thread(
            sandbox.fs.write, path=f"{fs_dir}/sub/existing.txt", content="e"
        )
    else:
        await sandbox.fs.write(path=f"{fs_dir}/sub/existing.txt", content="e")
    event = await _watch_until_written(
        sandbox, f"{fs_dir}/sub/nested.txt", path=fs_dir, recursive=True
    )
    assert {"CREATE", "WRITE"} & set(event.ops)


# Every push starts a build on the server, and CI runs this suite on every
# push, so a run pushes one image, built once and checked from both clients.
@dataclass
class _PushedImage:
    name: str
    builder: ImageBuilder
    sandbox_name: str
    """A sandbox created from the image."""


@pytest.fixture(scope="session")
def pushed_image() -> Iterator[_PushedImage]:
    _require_env()
    # Three CI jobs push per run already; the 3.11 one leaves it to them.
    if sys.version_info[:2] == (3, 11):
        pytest.skip("the image push runs only on the newest Python's CI jobs")
    client = _client()
    image_name = _unique()
    sandbox_name = _unique()
    sandbox_created = False
    # A pullable base, since the default sandbox image name only resolves on
    # sandbox create, not in a build. The label makes each run's build context
    # unique: identical pushes share one build on the server, which links
    # their images so none can be deleted before the others. The sandbox API
    # binary and entrypoint come from the builder's injection.
    builder = (
        ImageBuilder.from_registry("debian:bookworm-slim")
        .label({"e2e-image": image_name})
        .add_file("/hello.txt", "hello from the image")
        .run_commands("echo built by RUN > /run.txt")
    )
    try:
        pushed = client.images.push(name=image_name, builder=builder)
        assert (pushed.name, pushed.status) == (image_name, "BUILT")
        sandbox_created = True
        created = client.create(
            name=sandbox_name,
            image=f"{image_name}:latest",
            # TODO: Same temporary region pin as the shared sandbox.
            region="us-was-1",
            labels=_LABELS,
        )
        assert created.info.status == "DEPLOYED"
        yield _PushedImage(name=image_name, builder=builder, sandbox_name=sandbox_name)
    finally:
        # The image cannot be deleted while a sandbox uses it. Cleanup errors
        # are printed rather than raised, so they never hide the test's own
        # failure.
        try:
            if sandbox_created:
                _delete_and_wait(client, sandbox_name)
            client.images.delete(name=image_name)
        except (
            SandboxError,
            httpx.HTTPError,
            TimeoutError,
            pydantic.ValidationError,
        ) as err:
            if not (isinstance(err, SandboxAPIError) and err.status == 404):
                print(f"cleanup of image {image_name} failed: {err!r}")
        finally:
            client.close()


@pytest.mark.asyncio
async def test_image_pushed_is_found(
    client: SandboxClient | AsyncSandboxClient, pushed_image: _PushedImage
) -> None:
    name = pushed_image.name
    if isinstance(client, SandboxClient):
        info = await asyncio.to_thread(client.images.get_info, name=name)
        listed = await asyncio.to_thread(
            lambda: list(client.images.list(name_prefix=name))
        )
        tags = await asyncio.to_thread(lambda: list(client.images.list_tags(name=name)))
        logs = await asyncio.to_thread(client.images.logs, name=name)
    else:
        info = await client.images.get_info(name=name)
        listed = [image async for image in client.images.list(name_prefix=name)]
        tags = [tag async for tag in client.images.list_tags(name=name)]
        logs = await client.images.logs(name=name)
    assert info.status == "BUILT"
    assert [image.name for image in listed] == [name]
    assert tags
    # Not checked for content: some builds have no logs on the server, so only
    # the order is asserted.
    assert [line.timestamp for line in logs] == sorted(line.timestamp for line in logs)
    assert pushed_image.builder.dockerfile().endswith(
        "COPY --from=ghcr.io/blaxel-ai/sandbox:latest /sandbox-api "
        "/usr/local/bin/sandbox-api\n"
        'ENTRYPOINT ["/usr/local/bin/sandbox-api"]\n'
    )


@pytest.mark.asyncio
async def test_image_sandbox_has_its_files_and_sandbox_api(
    client: SandboxClient | AsyncSandboxClient, pushed_image: _PushedImage
) -> None:
    paths = ["/hello.txt", "/run.txt"]
    commands = ["test -x /usr/local/bin/sandbox-api", "tr '\\0' ' ' < /proc/1/cmdline"]
    if isinstance(client, SandboxClient):
        sandbox = await asyncio.to_thread(client.get, name=pushed_image.sandbox_name)
        contents = [
            await asyncio.to_thread(sandbox.fs.read, path=path) for path in paths
        ]
        processes = [
            await asyncio.to_thread(
                sandbox.process.exec, command=command, wait_for_completion=True
            )
            for command in commands
        ]
    else:
        async_sandbox = await client.get(name=pushed_image.sandbox_name)
        contents = [await async_sandbox.fs.read(path=path) for path in paths]
        processes = [
            await async_sandbox.process.exec(command=command, wait_for_completion=True)
            for command in commands
        ]
    assert contents == ["hello from the image", "built by RUN\n"]
    # The copied binary is where the copy put it, and runs as the entrypoint,
    # which is what answers these calls at all.
    assert processes[0].exit_code == 0
    assert re.match(r"/usr/local/bin/sandbox-api\b", processes[1].stdout)


def test_uses_http2_by_default(shared: _Shared) -> None:
    versions: list[str] = []
    with _client() as client:
        client.http_client.event_hooks["response"].append(
            lambda response: versions.append(response.http_version)
        )
        client.get_info(name=shared.name)
    # The token exchange and the call itself.
    assert versions == ["HTTP/2", "HTTP/2"]


def _unique() -> str:
    return f"py-e2e-{secrets.token_hex(4)}"


def _assert_within_run(since: datetime, timestamp: datetime) -> None:
    # Catches timestamp format and zone mistakes.
    assert since - _CLOCK_SKEW <= timestamp <= datetime.now(UTC) + _CLOCK_SKEW


async def _kill_quietly(sandbox: Sandbox | AsyncSandbox, identifier: str) -> None:
    # The test may have ended the process already, and cleanup never masks the
    # test's own failure.
    with contextlib.suppress(Exception):
        if isinstance(sandbox, Sandbox):
            await asyncio.to_thread(sandbox.process.kill, identifier=identifier)
        else:
            await sandbox.process.kill(identifier=identifier)


async def _mode(sandbox: Sandbox | AsyncSandbox, path: str) -> str:
    command = f"stat -c %a '{path}'"
    if isinstance(sandbox, Sandbox):
        process = await asyncio.to_thread(
            sandbox.process.exec, command=command, wait_for_completion=True
        )
    else:
        process = await sandbox.process.exec(command=command, wait_for_completion=True)
    return process.stdout.strip()


# How many times a watch test writes its file before giving up, and how long it
# waits for the event in all.
_WATCH_ATTEMPTS = 20
_WATCH_TIMEOUT = 60


async def _watch_until_written(
    sandbox: Sandbox | AsyncSandbox,
    file: str,
    *,
    path: str,
    recursive: bool | None = None,
) -> SandboxFileSystemWatchEvent:
    # The server's watch starts at some unknown point after the request, so a
    # change made too early is missed. Rather than sleeping, a writer repeats
    # the change, each attempt a full round trip, until the watch reports it.
    if isinstance(sandbox, Sandbox):
        return await asyncio.to_thread(
            _watch_until_written_sync, sandbox, file, path, recursive
        )
    found = asyncio.Event()

    async def write() -> None:
        for attempt in range(_WATCH_ATTEMPTS):
            if found.is_set():
                return
            await sandbox.fs.write(path=file, content=str(attempt))

    stream = await sandbox.fs.watch(path=path, recursive=recursive)
    writer = asyncio.create_task(write())
    try:
        async with stream, asyncio.timeout(_WATCH_TIMEOUT):
            async for event in stream:
                if event.path == file:
                    return event
        pytest.fail("the watch ended before an event for the file")
    finally:
        found.set()
        await writer


def _watch_until_written_sync(
    sandbox: Sandbox, file: str, path: str, recursive: bool | None
) -> SandboxFileSystemWatchEvent:
    found = threading.Event()
    with sandbox.fs.watch(path=path, recursive=recursive) as stream:

        def write() -> None:
            for attempt in range(_WATCH_ATTEMPTS):
                if found.is_set():
                    return
                sandbox.fs.write(path=file, content=str(attempt))
            # Every attempt went unseen, so the watch is ended from here, since
            # it would otherwise wait for an event forever. Only the response
            # can be closed from another thread, not the stream reading it.
            if not found.is_set():
                stream._response.close()

        writer = threading.Thread(target=write)
        writer.start()
        try:
            for event in stream:
                if event.path == file:
                    return event
            pytest.fail("the watch ended before an event for the file")
        finally:
            found.set()
            writer.join()


def _delete_and_wait(client: SandboxClient, name: str) -> None:
    # Cleanup only: a sandbox must be gone before its image can be deleted.
    client.delete(name=name)
    deadline = time.monotonic() + 120
    while True:
        try:
            info = client.get_info(name=name)
        except SandboxAPIError as err:
            if err.status == 404:
                return
            raise
        # A deleted sandbox stays listed as TERMINATED rather than
        # disappearing.
        if info.status == "TERMINATED":
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"sandbox {name} is still {info.status}")
        time.sleep(2)


def _require_env() -> None:
    if not API_KEY:
        pytest.skip("BASETEN_E2E_TEST_API_KEY not set")
    if not DOMAIN:
        raise OSError(
            "BASETEN_E2E_TEST_API_KEY is set but BASETEN_E2E_TEST_DOMAIN is missing"
        )


def _client() -> SandboxClient:
    if not DEBUG:
        return SandboxClient(api_key=API_KEY, base_url_override=f"https://api.{DOMAIN}")
    return SandboxClient(
        api_key=API_KEY,
        base_url_override=f"https://api.{DOMAIN}",
        http_client_override=httpx.Client(
            transport=_DebugTransport(http2=True), timeout=_DEBUG_TIMEOUT
        ),
        close_http_client_on_close=True,
    )


def _async_client() -> AsyncSandboxClient:
    if not DEBUG:
        return AsyncSandboxClient(
            api_key=API_KEY, base_url_override=f"https://api.{DOMAIN}"
        )
    return AsyncSandboxClient(
        api_key=API_KEY,
        base_url_override=f"https://api.{DOMAIN}",
        http_client_override=httpx.AsyncClient(
            transport=_AsyncDebugTransport(http2=True), timeout=_DEBUG_TIMEOUT
        ),
        close_http_client_on_close=True,
    )


class _LoggedStream(httpx.SyncByteStream):
    """Passes a response body through, logging it once fully read or closed."""

    def __init__(
        self, stream: httpx.SyncByteStream, log: Callable[[bytes], None]
    ) -> None:
        self._stream = stream
        self._log = log
        self._chunks: list[bytes] = []

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            self._chunks.append(chunk)
            yield chunk

    def close(self) -> None:
        self._stream.close()
        self._log(b"".join(self._chunks))


class _AsyncLoggedStream(httpx.AsyncByteStream):
    """Async form of :class:`_LoggedStream`."""

    def __init__(
        self, stream: httpx.AsyncByteStream, log: Callable[[bytes], None]
    ) -> None:
        self._stream = stream
        self._log = log
        self._chunks: list[bytes] = []

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._chunks.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()
        self._log(b"".join(self._chunks))


class _DebugTransport(httpx.HTTPTransport):
    """Prints each request and its response, credentials masked."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        response.stream = _LoggedStream(
            cast(httpx.SyncByteStream, response.stream),
            _response_logger(request, response),
        )
        return response


class _AsyncDebugTransport(httpx.AsyncHTTPTransport):
    """Async form of :class:`_DebugTransport`."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        response.stream = _AsyncLoggedStream(
            cast(httpx.AsyncByteStream, response.stream),
            _response_logger(request, response),
        )
        return response


def _response_logger(
    request: httpx.Request, response: httpx.Response
) -> Callable[[bytes], None]:
    """Return a function printing the request with the given response body."""
    headers = dict(request.headers)
    if "authorization" in headers:
        headers["authorization"] = "<masked>"
    try:
        request_body: Any = request.content.decode(errors="replace")
    except httpx.RequestNotRead:
        request_body = "<stream>"

    def log(body: bytes) -> None:
        # The token exchange's response body is the minted token.
        shown = (
            "<masked>"
            if request.url.path == "/v1/token"
            else body.decode(errors="replace")
        )
        print(
            request.method,
            request.url,
            {
                "request": {"headers": headers, "body": request_body},
                "response": {
                    "status": response.status_code,
                    "http_version": response.http_version,
                    "headers": dict(response.headers),
                    "body": shown,
                },
            },
        )

    return log

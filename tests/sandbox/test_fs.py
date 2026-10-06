from __future__ import annotations

import asyncio
import email.parser
import email.policy
import inspect
import itertools
import json
import queue
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import baseten.sandbox._retry
from baseten.sandbox import (
    AsyncSandbox,
    AsyncSandboxClient,
    Sandbox,
    SandboxAPIError,
    SandboxClient,
    SandboxError,
    SandboxFileSystemCopyError,
    SandboxFileSystemDirectory,
    SandboxFileSystemFileInfo,
    SandboxFileSystemFindMatch,
    SandboxFileSystemFindResult,
    SandboxFileSystemGrepMatch,
    SandboxFileSystemGrepResult,
    SandboxFileSystemSubdirectoryInfo,
    SandboxFileSystemWatchEvent,
)

_SANDBOX_URL = "https://sbx.test"

_PART_SIZE = 5 * 1024 * 1024

_Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@pytest.fixture(autouse=True)
def short_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries take milliseconds instead of seconds.
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 0.001)
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 0.002)


class _Fake:
    """Fake sandbox answering through a handler, recording requests.

    Assertions belong in the test, not in the handler.
    """

    def __init__(self, handler: _Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()

    def _record(self, request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/v1/token":
            return httpx.Response(
                200,
                json={
                    "token": "token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                },
            )
        with self._lock:
            self.requests.append(request)
        return None

    def sandbox(self, executor: ThreadPoolExecutor | None = None) -> Sandbox:
        def handle(request: httpx.Request) -> httpx.Response:
            request.read()
            token = self._record(request)
            if token is not None:
                return token
            response = self.handler(request)
            assert isinstance(response, httpx.Response)
            return response

        client = SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(transport=httpx.MockTransport(handle)),
            executor=executor,
        )
        return client.sandbox_from_url(url=_SANDBOX_URL)

    def async_sandbox(self) -> AsyncSandbox:
        async def handle(request: httpx.Request) -> httpx.Response:
            await request.aread()
            token = self._record(request)
            if token is not None:
                return token
            response = self.handler(request)
            if inspect.isawaitable(response):
                response = await response
            return response

        client = AsyncSandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )
        return client.sandbox_from_url(url=_SANDBOX_URL)


def _ok(body: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"message": "ok"} if body is None else body)


def _replies(*responses: httpx.Response) -> _Handler:
    """Answer with each response in turn, repeating the last."""
    remaining = list(responses)

    def handle(_: httpx.Request) -> httpx.Response:
        response = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        # A response is single use, so each request gets a copy.
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    return handle


def _multipart(
    part: Callable[[int], httpx.Response | None] | None = None,
) -> _Handler:
    """Fake the multipart upload endpoints.

    Uploads are numbered from 1 and each part is answered with
    ``etag-<n>``. *part*, when set, runs first for each part and answers it
    instead when it returns a response.
    """
    uploads = itertools.count(1)

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/filesystem-multipart/initiate/"):
            return _ok({"uploadId": f"upload-{next(uploads)}"})
        if path.endswith("/part"):
            part_number = int(request.url.params["partNumber"])
            response = None if part is None else part(part_number)
            if response is not None:
                return response
            return _ok({"etag": f"etag-{part_number}", "partNumber": part_number})
        return _ok()

    return handle


def _path(request: httpx.Request) -> str:
    """Give a request's path as sent, percent-encoding included."""
    return request.url.raw_path.decode().partition("?")[0]


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _form(request: httpx.Request) -> tuple[str | None, bytes, dict[str, str]]:
    """Give a multipart request's file name, file content, and other fields."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        + request.content
    )
    filename: str | None = None
    content = b""
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        if name == "file":
            filename, content = part.get_filename(), payload
        else:
            fields[str(name)] = payload.decode()
    return filename, content, fields


def _uploaded(requests: list[httpx.Request]) -> bytes:
    """Join the content of every part request in part number order."""
    parts: dict[int, bytes] = {}
    for request in requests:
        if _path(request).endswith("/part"):
            _, content, _ = _form(request)
            assert len(content) <= _PART_SIZE
            parts[int(request.url.params["partNumber"])] = content
    assert sorted(parts) == list(range(1, len(parts) + 1))
    return b"".join(parts[number] for number in sorted(parts))


def _content(size: int) -> bytes:
    return bytes(index % 251 for index in range(size))


_DIRECTORY = {
    "name": "tmp",
    "path": "/tmp",
    "files": [
        {
            "name": "a.txt",
            "path": "/tmp/a.txt",
            "size": 5,
            "permissions": "-rw-r--r--",
            "owner": "root",
            "group": "root",
            "lastModified": "Wed, 01 Jan 2023 12:00:00 GMT",
        }
    ],
    "subdirectories": [{"name": "d", "path": "/tmp/d"}],
}


def _process(**fields: Any) -> dict[str, Any]:
    return {
        "pid": "7",
        "name": "",
        "command": "cp",
        "status": "completed",
        "exitCode": 0,
        "stdout": "",
        "stderr": "",
        "logs": "",
        "workingDir": "/",
        "startedAt": "Wed, 01 Jan 2023 12:00:00 GMT",
        "completedAt": "Wed, 01 Jan 2023 12:00:01 GMT",
        **fields,
    }


def _watch_lines(*lines: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/plain"},
        content="".join(line + "\n" for line in lines),
    )


class TestSandboxFileSystem:
    def test_reads_text_escaping_the_path(self) -> None:
        fake = _Fake(
            _replies(
                _ok(
                    {
                        "name": "a",
                        "path": "/tmp/a b%.txt",
                        "content": "hello",
                        "group": "g",
                        "lastModified": "",
                        "owner": "o",
                        "permissions": "p",
                        "size": 5,
                    }
                )
            )
        )
        assert fake.sandbox().fs.read(path="/tmp/a b%.txt") == "hello"
        assert _path(fake.requests[0]) == "/filesystem/%2Ftmp%2Fa%20b%25.txt"

    def test_fails_to_read_a_directory(self) -> None:
        fake = _Fake(_replies(_ok(_DIRECTORY)))
        with pytest.raises(SandboxError, match="is a directory"):
            fake.sandbox().fs.read(path="/tmp")

    def test_reads_bytes_as_octets(self) -> None:
        fake = _Fake(
            _replies(
                httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream"},
                    content=b"\x00\x01\xff",
                )
            )
        )
        assert fake.sandbox().fs.read_bytes(path="/tmp/bin") == b"\x00\x01\xff"
        assert fake.requests[0].headers["Accept"] == "application/octet-stream"

    def test_fails_to_read_a_directory_as_bytes(self) -> None:
        fake = _Fake(_replies(_ok(_DIRECTORY)))
        with pytest.raises(SandboxError, match="is a directory"):
            fake.sandbox().fs.read_bytes(path="/tmp")

    def test_writes_text_retrying_a_gateway_error(self) -> None:
        fake = _Fake(_replies(httpx.Response(502, json={}), _ok()))
        fake.sandbox().fs.write(path="/tmp/a", content="")
        assert len(fake.requests) == 2
        assert fake.requests[1].method == "PUT"
        # Empty content is still sent, so the file ends up empty.
        assert _body(fake.requests[1]) == {"content": ""}

    def test_writes_text_up_to_5mb_of_utf8_in_one_request(self) -> None:
        fake = _Fake(_replies(_ok()))
        fake.sandbox().fs.write(path="/tmp/a", content="é" * (_PART_SIZE // 2))
        assert len(fake.requests) == 1

    def test_writes_text_over_5mb_of_utf8_in_parts_without_permissions(self) -> None:
        fake = _Fake(_multipart())
        content = "é" * (_PART_SIZE // 2 + 1)
        fake.sandbox().fs.write(path="/tmp/a", content=content)
        assert _path(fake.requests[0]) == "/filesystem-multipart/initiate/%2Ftmp%2Fa"
        assert _body(fake.requests[0]) == {}
        assert _uploaded(fake.requests) == content.encode()

    def test_writes_bytes_as_a_form_rebuilt_for_each_attempt(self) -> None:
        fake = _Fake(_replies(httpx.Response(503, json={}), _ok()))
        fake.sandbox().fs.write_bytes(
            path="/tmp/run.sh", content=b"\x01\x02\x03", permissions="0755"
        )
        assert len(fake.requests) == 2
        for request in fake.requests:
            assert request.method == "PUT"
            assert _path(request) == "/filesystem/%2Ftmp%2Frun.sh"
            assert request.headers["Authorization"] == "Bearer token"
            assert _form(request) == (
                "run.sh",
                b"\x01\x02\x03",
                {"permissions": "0755", "path": "/tmp/run.sh"},
            )

    def test_writes_bytes_without_permissions_when_unset(self) -> None:
        fake = _Fake(_replies(_ok()))
        fake.sandbox().fs.write_bytes(path="/tmp/a", content=b"x")
        assert _form(fake.requests[0])[2] == {"path": "/tmp/a"}

    def test_fails_a_byte_write_with_the_sandboxs_error(self) -> None:
        fake = _Fake(_replies(httpx.Response(422, json={"error": "bad path"})))
        with pytest.raises(SandboxAPIError, match="bad path") as raised:
            fake.sandbox().fs.write_bytes(path="/tmp/a", content=b"x")
        assert raised.value.status == 422

    def test_writes_exactly_5mb_of_bytes_in_one_request(self) -> None:
        fake = _Fake(_replies(_ok()))
        fake.sandbox().fs.write_bytes(path="/tmp/a", content=bytes(_PART_SIZE))
        assert len(fake.requests) == 1

    def test_writes_bytes_over_5mb_in_parts_every_byte_once(self) -> None:
        fake = _Fake(_multipart())
        content = _content(2 * _PART_SIZE + 17)
        fake.sandbox().fs.write_bytes(
            path="/tmp/big", content=content, permissions="0755"
        )
        assert _body(fake.requests[0]) == {"permissions": "0755"}
        assert _uploaded(fake.requests) == content
        complete = fake.requests[-1]
        assert _path(complete) == "/filesystem-multipart/upload-1/complete"
        assert _body(complete) == {
            "parts": [
                {"partNumber": 1, "etag": "etag-1"},
                {"partNumber": 2, "etag": "etag-2"},
                {"partNumber": 3, "etag": "etag-3"},
            ]
        }

    def test_retries_a_part_after_a_gateway_error_rebuilding_its_form(self) -> None:
        failed = threading.Event()

        def part(part_number: int) -> httpx.Response | None:
            if part_number == 2 and not failed.is_set():
                failed.set()
                return httpx.Response(502, json={})
            return None

        fake = _Fake(_multipart(part))
        content = bytes(_PART_SIZE) + b"\x07"
        fake.sandbox().fs.write_bytes(path="/tmp/a", content=content)
        part_two = [
            _form(request)[1]
            for request in fake.requests
            if _path(request).endswith("/part")
            and request.url.params["partNumber"] == "2"
        ]
        assert part_two == [b"\x07", b"\x07"]

    def test_aborts_the_upload_when_a_part_fails_with_the_parts_error(self) -> None:
        fake = _Fake(
            _multipart(
                lambda part_number: (
                    httpx.Response(500, json={"error": "disk full"})
                    if part_number == 1
                    else None
                )
            )
        )
        with pytest.raises(SandboxAPIError, match="disk full"):
            fake.sandbox().fs.write_bytes(path="/tmp/a", content=bytes(_PART_SIZE + 1))
        paths = [_path(request) for request in fake.requests]
        assert paths[-1] == "/filesystem-multipart/upload-1/abort"
        assert not any(path.endswith("/complete") for path in paths)

    def test_aborts_when_completing_fails_keeping_that_error(self) -> None:
        multipart = _multipart()

        def handle(
            request: httpx.Request,
        ) -> httpx.Response | Awaitable[httpx.Response]:
            if request.url.path.endswith("/complete"):
                return httpx.Response(500, json={"error": "complete failed"})
            if request.url.path.endswith("/abort"):
                return httpx.Response(500, json={"error": "abort failed"})
            return multipart(request)

        fake = _Fake(handle)
        with pytest.raises(SandboxAPIError, match="complete failed"):
            fake.sandbox().fs.write_bytes(path="/tmp/a", content=bytes(_PART_SIZE + 1))
        assert _path(fake.requests[-1]) == "/filesystem-multipart/upload-1/abort"

    def test_keeps_at_most_2_parts_in_flight_per_sandbox_across_its_uploads(
        self,
    ) -> None:
        uploads = 2
        parts_per_upload = 3
        lock = threading.Lock()
        in_flight = 0
        max_in_flight = 0
        arrived: queue.Queue[None] = queue.Queue()
        release = threading.Semaphore(0)

        def part(_: int) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            arrived.put(None)
            release.acquire()
            with lock:
                in_flight -= 1

        sandbox = _Fake(_multipart(part)).sandbox()
        with ThreadPoolExecutor(max_workers=uploads) as callers:
            writes = [
                callers.submit(
                    sandbox.fs.write_bytes,
                    path=f"/tmp/{index}",
                    content=bytes(2 * _PART_SIZE + 1),
                )
                for index in range(uploads)
            ]
            # Two parts are held from the start, and each release lets one
            # waiting part in, so parts beyond the limit would arrive while
            # two are held.
            arrived.get()
            arrived.get()
            for _ in range(uploads * parts_per_upload - 2):
                release.release()
                arrived.get()
            release.release(2)
            for write in writes:
                write.result()
        assert max_in_flight == 2

    def test_gives_each_sandbox_its_own_limit_of_parts_in_flight(self) -> None:
        arrived: queue.Queue[None] = queue.Queue()
        release = threading.Event()

        def part(_: int) -> None:
            arrived.put(None)
            release.wait()

        fake = _Fake(_multipart(part))
        sandboxes = [fake.sandbox(), fake.sandbox()]
        with ThreadPoolExecutor(max_workers=2) as callers:
            writes = [
                callers.submit(
                    sandbox.fs.write_bytes, path="/tmp/a", content=bytes(_PART_SIZE + 1)
                )
                for sandbox in sandboxes
            ]
            # Four parts in flight at once, two per sandbox.
            for _ in range(4):
                arrived.get()
            release.set()
            for write in writes:
                write.result()

    def test_sends_parts_through_the_callers_thread_pool(self) -> None:
        threads: set[str] = set()
        lock = threading.Lock()

        def part(_: int) -> None:
            with lock:
                threads.add(threading.current_thread().name)

        with ThreadPoolExecutor(thread_name_prefix="caller-pool") as pool:
            sandbox = _Fake(_multipart(part)).sandbox(executor=pool)
            sandbox.fs.write_bytes(path="/tmp/a", content=bytes(3 * _PART_SIZE))
        assert any(name.startswith("caller-pool") for name in threads)

    def test_uploads_from_a_busy_callers_pool_without_deadlocking(self) -> None:
        # The pool's one thread runs the write itself, so only the calling
        # thread is left to send parts.
        with ThreadPoolExecutor(max_workers=1) as pool:
            fake = _Fake(_multipart())
            sandbox = fake.sandbox(executor=pool)
            content = _content(2 * _PART_SIZE + 1)
            pool.submit(sandbox.fs.write_bytes, path="/tmp/a", content=content).result(
                timeout=30
            )
        assert _uploaded(fake.requests) == content

    def test_makes_a_directory_with_permissions_only_when_set(self) -> None:
        fake = _Fake(_replies(_ok()))
        fake.sandbox().fs.mkdir(path="/tmp/d")
        fake.sandbox().fs.mkdir(path="/tmp/d", permissions="0700")
        assert _body(fake.requests[0]) == {"isDirectory": True}
        assert _body(fake.requests[1]) == {"isDirectory": True, "permissions": "0700"}

    def test_lists_a_directory(self) -> None:
        fake = _Fake(_replies(_ok(_DIRECTORY)))
        assert fake.sandbox().fs.list(path="/tmp") == SandboxFileSystemDirectory(
            name="tmp",
            path="/tmp",
            files=[
                SandboxFileSystemFileInfo(
                    name="a.txt",
                    path="/tmp/a.txt",
                    size_bytes=5,
                    permissions="-rw-r--r--",
                    owner="root",
                    group="root",
                    last_modified=datetime(2023, 1, 1, 12, tzinfo=UTC),
                )
            ],
            subdirectories=[SandboxFileSystemSubdirectoryInfo(name="d", path="/tmp/d")],
        )

    def test_fails_to_list_a_file(self) -> None:
        fake = _Fake(
            _replies(
                _ok(
                    {
                        "name": "a",
                        "path": "/tmp/a",
                        "content": "x",
                        "group": "g",
                        "lastModified": "",
                        "owner": "o",
                        "permissions": "p",
                        "size": 1,
                    }
                )
            )
        )
        with pytest.raises(SandboxError, match="is a file"):
            fake.sandbox().fs.list(path="/tmp/a")

    def test_removes_without_retrying_sending_recursive_only_when_set(self) -> None:
        fake = _Fake(_replies(httpx.Response(502, json={}), _ok()))
        with pytest.raises(SandboxAPIError):
            fake.sandbox().fs.remove(path="/tmp/a")
        fake.sandbox().fs.remove(path="/tmp/d", recursive=True)
        assert len(fake.requests) == 2
        assert fake.requests[0].url.query == b""
        assert fake.requests[1].url.params["recursive"] == "true"

    def test_finds_entries_with_every_option_mapped(self) -> None:
        fake = _Fake(
            _replies(_ok({"matches": [{"path": "a.py", "type": "file"}], "total": 3}))
        )
        result = fake.sandbox().fs.find(
            path="/app",
            type="file",
            patterns=["*.py", "*.txt"],
            max_results=0,
            exclude_dirs=["node_modules", ".git"],
            exclude_hidden=False,
        )
        assert result == SandboxFileSystemFindResult(
            matches=[SandboxFileSystemFindMatch(path="a.py", type="file")], total=3
        )
        assert dict(fake.requests[0].url.params) == {
            "type": "file",
            "patterns": "*.py,*.txt",
            "maxResults": "0",
            "excludeDirs": "node_modules,.git",
            "excludeHidden": "false",
        }

    def test_finds_with_no_options_sent_when_none_are_set(self) -> None:
        fake = _Fake(_replies(_ok({"matches": [], "total": 0})))
        fake.sandbox().fs.find(path="/app")
        assert fake.requests[0].url.query == b""

    def test_greps_with_every_option_mapped(self) -> None:
        fake = _Fake(
            _replies(
                _ok(
                    {
                        "matches": [
                            {
                                "path": "a.py",
                                "line": 2,
                                "column": 5,
                                "text": "find Needle here",
                                "context": "one\nfind Needle here\nthree",
                            }
                        ],
                        "total": 1,
                        "query": "Needle",
                    }
                )
            )
        )
        result = fake.sandbox().fs.grep(
            path="/app",
            query="Needle",
            case_sensitive=True,
            max_results=5,
            file_pattern="*.py",
            exclude_dirs=["dist"],
            context_lines=1,
        )
        assert result == SandboxFileSystemGrepResult(
            matches=[
                SandboxFileSystemGrepMatch(
                    path="a.py",
                    line=2,
                    column=5,
                    text="find Needle here",
                    context="one\nfind Needle here\nthree",
                )
            ],
            total=1,
        )
        assert dict(fake.requests[0].url.params) == {
            "query": "Needle",
            "caseSensitive": "true",
            "maxResults": "5",
            "filePattern": "*.py",
            "excludeDirs": "dist",
            "contextLines": "1",
        }

    def test_copies_by_running_cp_with_both_paths_quoted_then_waiting(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return _ok(_process(status="running", completedAt=""))
            return _ok(_process())

        fake = _Fake(handle)
        fake.sandbox().fs.copy(source="/tmp/it's", destination="/tmp/b c")
        assert _body(fake.requests[0])["command"] == "cp -r '/tmp/it'\\''s' '/tmp/b c'"
        assert _path(fake.requests[1]) == "/process/7"

    @pytest.mark.parametrize(
        ("finished", "message"),
        [
            (
                {"status": "failed", "exitCode": 1, "stderr": "cp: no such file\n"},
                "ended failed: cp: no such file",
            ),
            ({"status": "killed", "exitCode": 0}, "ended killed: exit code 0"),
        ],
    )
    def test_fails_a_copy_whose_cp_does_not_complete(
        self, finished: dict[str, Any], message: str
    ) -> None:
        fake = _Fake(_replies(_ok(_process(**finished))))
        with pytest.raises(SandboxFileSystemCopyError, match=message) as raised:
            fake.sandbox().fs.copy(source="/a", destination="/b")
        assert (raised.value.source, raised.value.destination) == ("/a", "/b")
        assert raised.value.process.status == finished["status"]

    def test_writes_a_tree_retrying_a_gateway_error(self) -> None:
        fake = _Fake(_replies(httpx.Response(504, json={}), _ok(_DIRECTORY)))
        fake.sandbox().fs.write_tree(path="/app", files={"a/b.txt": "hi"})
        assert len(fake.requests) == 2
        assert _path(fake.requests[1]) == "/filesystem/tree/%2Fapp"
        assert _body(fake.requests[1]) == {"files": {"a/b.txt": "hi"}}

    def test_watches_joining_each_events_directory_and_name(self) -> None:
        fake = _Fake(
            _replies(
                _watch_lines(
                    '{"op":"CREATE","path":"/app/","name":"a"}',
                    "[keepalive]",
                    "",
                    '{"op":"WRITE|CHMOD","path":"/app","name":"b"}',
                )
            )
        )
        with fake.sandbox().fs.watch(
            path="/app", ignore=["node_modules", ".git"]
        ) as stream:
            events = list(stream)
        assert events == [
            SandboxFileSystemWatchEvent(ops=["CREATE"], path="/app/a"),
            SandboxFileSystemWatchEvent(ops=["WRITE", "CHMOD"], path="/app/b"),
        ]
        assert _path(fake.requests[0]) == "/watch/filesystem/%2Fapp"
        assert fake.requests[0].url.params["ignore"] == "node_modules,.git"

    def test_watches_recursively_by_appending_a_suffix_to_the_path(self) -> None:
        fake = _Fake(_replies(_watch_lines()))
        with fake.sandbox().fs.watch(path="/app/", recursive=True) as stream:
            list(stream)
        assert _path(fake.requests[0]) == "/watch/filesystem/%2Fapp%2F%2A%2A"

    def test_fails_a_watch_on_an_unparseable_line(self) -> None:
        fake = _Fake(_replies(_watch_lines("not json")))
        with (
            fake.sandbox().fs.watch(path="/app") as stream,
            pytest.raises(json.JSONDecodeError),
        ):
            list(stream)


class TestAsyncSandboxFileSystem:
    @pytest.mark.asyncio
    async def test_reads_writes_and_lists(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/filesystem/tree/"):
                return _ok(_DIRECTORY)
            if request.method == "PUT":
                return _ok()
            if request.headers["Accept"] == "application/octet-stream":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream"},
                    content=b"\x00\xff",
                )
            return _ok(_DIRECTORY)

        fs = _Fake(handle).async_sandbox().fs
        await fs.write(path="/tmp/a", content="hi")
        await fs.write_bytes(path="/tmp/b", content=b"\x00\xff")
        await fs.mkdir(path="/tmp/d")
        await fs.write_tree(path="/tmp", files={"c": "x"})
        assert await fs.read_bytes(path="/tmp/b") == b"\x00\xff"
        assert (await fs.list(path="/tmp")).subdirectories == [
            SandboxFileSystemSubdirectoryInfo(name="d", path="/tmp/d")
        ]

    @pytest.mark.asyncio
    async def test_writes_bytes_over_5mb_in_parts_every_byte_once(self) -> None:
        fake = _Fake(_multipart())
        content = _content(2 * _PART_SIZE + 17)
        await fake.async_sandbox().fs.write_bytes(path="/tmp/big", content=content)
        assert _uploaded(fake.requests) == content
        assert _path(fake.requests[-1]) == "/filesystem-multipart/upload-1/complete"

    @pytest.mark.asyncio
    async def test_aborts_the_upload_when_a_part_fails(self) -> None:
        fake = _Fake(
            _multipart(
                lambda part_number: (
                    httpx.Response(500, json={"error": "disk full"})
                    if part_number == 2
                    else None
                )
            )
        )
        with pytest.raises(SandboxAPIError, match="disk full"):
            await fake.async_sandbox().fs.write_bytes(
                path="/tmp/a", content=bytes(_PART_SIZE + 1)
            )
        paths = [_path(request) for request in fake.requests]
        assert paths[-1] == "/filesystem-multipart/upload-1/abort"
        assert not any(path.endswith("/complete") for path in paths)

    @pytest.mark.asyncio
    async def test_aborts_the_upload_when_cancelled(self) -> None:
        arrived = asyncio.Event()
        multipart = _multipart()

        async def held_part() -> httpx.Response:
            arrived.set()
            await asyncio.Event().wait()
            raise AssertionError("never released")

        def handle(
            request: httpx.Request,
        ) -> httpx.Response | Awaitable[httpx.Response]:
            if request.url.path.endswith("/part"):
                return held_part()
            return multipart(request)

        fake = _Fake(handle)
        write = asyncio.create_task(
            fake.async_sandbox().fs.write_bytes(
                path="/tmp/a", content=bytes(_PART_SIZE + 1)
            )
        )
        await arrived.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert _path(fake.requests[-1]) == "/filesystem-multipart/upload-1/abort"

    @pytest.mark.asyncio
    async def test_keeps_at_most_2_parts_in_flight_per_sandbox(self) -> None:
        in_flight = 0
        max_in_flight = 0
        arrived: asyncio.Queue[None] = asyncio.Queue()
        release = asyncio.Semaphore(0)
        multipart = _multipart()

        async def held_part(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            arrived.put_nowait(None)
            await release.acquire()
            in_flight -= 1
            response = multipart(request)
            assert isinstance(response, httpx.Response)
            return response

        def handle(
            request: httpx.Request,
        ) -> httpx.Response | Awaitable[httpx.Response]:
            if request.url.path.endswith("/part"):
                return held_part(request)
            return multipart(request)

        fs = _Fake(handle).async_sandbox().fs
        writes = asyncio.gather(
            *(
                fs.write_bytes(path=f"/tmp/{index}", content=bytes(2 * _PART_SIZE + 1))
                for index in range(2)
            )
        )
        # As the sync test: parts beyond the limit would arrive while two are
        # held.
        await arrived.get()
        await arrived.get()
        for _ in range(4):
            release.release()
            await arrived.get()
        release.release()
        release.release()
        await writes
        assert max_in_flight == 2

    @pytest.mark.asyncio
    async def test_copies_and_watches(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/watch/"):
                return _watch_lines('{"op":"REMOVE","path":"/app","name":"a"}')
            return _ok(_process())

        fs = _Fake(handle).async_sandbox().fs
        await fs.copy(source="/a", destination="/b")
        async with await fs.watch(path="/app") as stream:
            events = [event async for event in stream]
        assert events == [SandboxFileSystemWatchEvent(ops=["REMOVE"], path="/app/a")]

from __future__ import annotations

import asyncio
import json
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
    SandboxGatewayError,
    SandboxProcessExecEventExit,
    SandboxProcessExecEventOutput,
    SandboxProcessInfo,
    SandboxProcessLogLine,
    SandboxProcessLogs,
    SandboxProcessWaitTimeoutError,
)

_SANDBOX_URL = "https://sbx.test"

_RESET = httpx.RemoteProtocolError("Server disconnected without sending a response.")

_Reply = httpx.Response | Exception | dict[str, Any] | list[Any]


@pytest.fixture(autouse=True)
def short_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries take milliseconds instead of seconds.
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 0.001)
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 0.002)


class _Api:
    """Fake sandbox replying from per-route queues, recording requests.

    Each route replies in order and repeats its last reply.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[_Reply]] = {}

    def route(self, method: str, path: str, *replies: _Reply) -> None:
        self.routes[(method, path)] = list(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return httpx.Response(
                200,
                json={
                    "token": "token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                },
            )
        self.requests.append(request)
        replies = self.routes[(request.method, request.url.path)]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            # A response is single use, so each request gets a copy.
            return httpx.Response(
                reply.status_code, headers=reply.headers, content=reply.content
            )
        return httpx.Response(200, json=reply)

    def sandbox(self) -> Sandbox:
        client = SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
        )
        return client.sandbox_from_url(url=_SANDBOX_URL)

    def async_sandbox(self) -> AsyncSandbox:
        async def handle(request: httpx.Request) -> httpx.Response:
            return self.handle(request)

        client = AsyncSandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )
        return client.sandbox_from_url(url=_SANDBOX_URL)


def _process(**fields: Any) -> dict[str, Any]:
    return {
        "pid": "42",
        "name": "job",
        "command": "echo hi",
        "status": "completed",
        "exitCode": 0,
        "stdout": "hi\n",
        "stderr": "",
        "logs": "hi\n",
        "workingDir": "/app",
        "startedAt": "Wed, 01 Jan 2023 12:00:00 GMT",
        "completedAt": "Wed, 01 Jan 2023 12:00:01 GMT",
        **fields,
    }


def _error(status: int, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": message})


def _ndjson(*records: dict[str, Any]) -> httpx.Response:
    content = "".join(json.dumps(record) + "\n" for record in records)
    return httpx.Response(
        200, headers={"Content-Type": "application/x-ndjson"}, content=content
    )


def _text(content: str) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=content)


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


class TestSandboxProcess:
    def test_execs_with_every_field_mapped(self) -> None:
        api = _Api()
        api.route("POST", "/process", _process())
        info = api.sandbox().process.exec(
            command="echo hi",
            name="job",
            working_dir="/app",
            env={"A": "1"},
            timeout=timedelta(milliseconds=1500),
            wait_for_completion=True,
            keep_alive=True,
            max_restarts=-1,
            restart_on_failure=True,
            stdin=True,
            wait_for_ports=[3000],
        )
        assert _body(api.requests[0]) == {
            "command": "echo hi",
            "name": "job",
            "workingDir": "/app",
            "env": {"A": "1"},
            # Rounded up to whole seconds.
            "timeout": 2,
            "waitForCompletion": True,
            "keepAlive": True,
            "maxRestarts": -1,
            "restartOnFailure": True,
            "stdin": True,
            "waitForPorts": [3000],
        }
        assert info == SandboxProcessInfo(
            pid="42",
            name="job",
            command="echo hi",
            status="completed",
            exit_code=0,
            stdout="hi\n",
            stderr="",
            logs="hi\n",
            working_dir="/app",
            started_at=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC),
            completed_at=datetime(2023, 1, 1, 12, 0, 1, tzinfo=UTC),
            keep_alive=None,
            max_restarts=None,
            restart_count=None,
            restart_on_failure=None,
            stdin=None,
        )

    def test_execs_with_only_set_fields(self) -> None:
        api = _Api()
        api.route("POST", "/process", _process(status="running", completedAt=""))
        info = api.sandbox().process.exec(command="sleep 1")
        assert _body(api.requests[0]) == {"command": "sleep 1"}
        assert info.completed_at is None

    def test_never_retries_exec(self) -> None:
        api = _Api()
        api.route("POST", "/process", _error(502), _process())
        with pytest.raises(SandboxGatewayError):
            api.sandbox().process.exec(command="echo hi")
        assert len(api.requests) == 1

    def test_converts_exec_error_response(self) -> None:
        api = _Api()
        api.route("POST", "/process", _error(422, "bad command"))
        with pytest.raises(SandboxAPIError, match="bad command") as raised:
            api.sandbox().process.exec(command="")
        assert raised.value.status == 422

    def test_exec_streams_output_then_exit(self) -> None:
        api = _Api()
        api.route(
            "POST",
            "/process",
            _ndjson(
                {"type": "stdout", "data": "Name? "},
                {"type": "stderr", "data": "warn\n"},
                {"type": "keepalive"},
                {"type": "unknown", "data": "skipped"},
                {"type": "result", "data": json.dumps(_process(exitCode=3))},
                {"type": "stdout", "data": "never read"},
            ),
        )
        with api.sandbox().process.exec_stream(
            command="ask", timeout=timedelta(0)
        ) as stream:
            events = list(stream)
        request = api.requests[0]
        assert request.headers["Accept"] == "application/x-ndjson"
        assert _body(request) == {
            "command": "ask",
            "timeout": 0,
            "waitForCompletion": True,
        }
        assert events[:2] == [
            SandboxProcessExecEventOutput(stream="stdout", text="Name? "),
            SandboxProcessExecEventOutput(stream="stderr", text="warn\n"),
        ]
        assert len(events) == 3
        assert isinstance(events[2], SandboxProcessExecEventExit)
        assert events[2].process.exit_code == 3

    def test_exec_stream_fails_without_exit(self) -> None:
        api = _Api()
        api.route("POST", "/process", _ndjson({"type": "stdout", "data": "hi"}))
        stream = api.sandbox().process.exec_stream(command="echo hi")
        assert next(stream) == SandboxProcessExecEventOutput(stream="stdout", text="hi")
        with pytest.raises(SandboxError, match="ended before reporting"):
            next(stream)

    def test_exec_stream_fails_on_error_record(self) -> None:
        api = _Api()
        api.route(
            "POST",
            "/process",
            _ndjson(
                {"type": "stdout", "data": "hi"},
                {"type": "error", "data": "process lost"},
                {"type": "stdout", "data": "never read"},
            ),
        )
        stream = api.sandbox().process.exec_stream(command="echo hi")
        assert next(stream) == SandboxProcessExecEventOutput(stream="stdout", text="hi")
        with pytest.raises(SandboxError, match="process stream failed: process lost"):
            next(stream)

    def test_exec_stream_raises_error_response_at_call(self) -> None:
        api = _Api()
        api.route("POST", "/process", _error(400, "bad request"))
        with pytest.raises(SandboxAPIError, match="bad request"):
            api.sandbox().process.exec_stream(command="echo hi")

    def test_gets_process_retrying_dropped_connection(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _RESET, _process())
        info = api.sandbox().process.get(identifier="job")
        assert info.pid == "42"
        assert len(api.requests) == 2

    def test_lists_processes(self) -> None:
        api = _Api()
        api.route("GET", "/process", [_process(pid="1"), _process(pid="2")])
        processes = api.sandbox().process.list()
        assert [process.pid for process in processes] == ["1", "2"]

    def test_stops_and_kills(self) -> None:
        api = _Api()
        api.route("DELETE", "/process/job", {"message": "ok"})
        api.route("DELETE", "/process/job/kill", {"message": "ok"})
        sandbox = api.sandbox()
        sandbox.process.stop(identifier="job")
        sandbox.process.kill(identifier="job")
        assert [request.url.path for request in api.requests] == [
            "/process/job",
            "/process/job/kill",
        ]

    def test_never_retries_stop(self) -> None:
        api = _Api()
        api.route("DELETE", "/process/job", _RESET, {"message": "ok"})
        with pytest.raises(httpx.RemoteProtocolError):
            api.sandbox().process.stop(identifier="job")
        assert len(api.requests) == 1

    def test_gets_logs(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/process/job/logs",
            {"stdout": "a\n", "stderr": "b\n", "logs": "a\nb\n"},
        )
        logs = api.sandbox().process.logs(identifier="job")
        assert logs == SandboxProcessLogs(stdout="a\n", stderr="b\n", logs="a\nb\n")

    def test_streams_logs_by_line(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/process/job/logs/stream",
            _text("stdout:one\n[keepalive]\nstderr:two\nplain\nstdout:"),
        )
        with api.sandbox().process.stream_logs(identifier="job") as stream:
            lines = list(stream)
        assert lines == [
            SandboxProcessLogLine(stream="stdout", text="one"),
            SandboxProcessLogLine(stream="stderr", text="two"),
            SandboxProcessLogLine(stream=None, text="plain"),
            SandboxProcessLogLine(stream="stdout", text=""),
        ]

    def test_waits_until_finished_riding_out_transient_errors(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/process/job",
            _process(status="running", completedAt=""),
            _error(503),
            _RESET,
            _error(500),
            _process(status="failed", exitCode=1),
        )
        info = api.sandbox().process.wait(
            identifier="job", poll_interval=timedelta(milliseconds=1)
        )
        # A failed process is returned, not raised.
        assert (info.status, info.exit_code) == ("failed", 1)
        assert len(api.requests) == 5

    def test_wait_raises_error_it_cannot_ride_out(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _error(404, "process not found"))
        with pytest.raises(SandboxAPIError, match="process not found"):
            api.sandbox().process.wait(identifier="job")

    def test_wait_raises_on_unknown_status(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _process(status="paused"))
        with pytest.raises(SandboxError, match="process job has unknown status paused"):
            api.sandbox().process.wait(identifier="job")

    def test_wait_times_out_with_last_error_as_cause(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _process(status="running"), _error(503))
        with pytest.raises(SandboxProcessWaitTimeoutError) as raised:
            api.sandbox().process.wait(
                identifier="job",
                timeout=timedelta(milliseconds=20),
                poll_interval=timedelta(milliseconds=1),
            )
        assert raised.value.identifier == "job"
        assert isinstance(raised.value.__cause__, SandboxGatewayError)

    def test_writes_stdin_as_bytes(self) -> None:
        api = _Api()
        api.route("POST", "/process/job/stdin", {"message": "ok"})
        sandbox = api.sandbox()
        sandbox.process.write_stdin(identifier="job", data="é\n")
        sandbox.process.write_stdin(identifier="job", data=b"\x00\xff")
        assert [request.content for request in api.requests] == [
            "é\n".encode(),
            b"\x00\xff",
        ]
        assert api.requests[0].headers["Content-Type"] == "application/octet-stream"

    def test_never_retries_write_stdin(self) -> None:
        api = _Api()
        api.route("POST", "/process/job/stdin", _error(502), {"message": "ok"})
        with pytest.raises(SandboxGatewayError):
            api.sandbox().process.write_stdin(identifier="job", data="x")
        assert len(api.requests) == 1

    def test_closes_stdin_retrying_gateway_error(self) -> None:
        api = _Api()
        api.route("DELETE", "/process/job/stdin", _error(502), {"message": "ok"})
        api.sandbox().process.close_stdin(identifier="job")
        assert len(api.requests) == 2


class TestAsyncSandboxProcess:
    @pytest.mark.asyncio
    async def test_execs_and_gets(self) -> None:
        api = _Api()
        api.route("POST", "/process", _process())
        api.route("GET", "/process/job", _RESET, _process())
        sandbox = api.async_sandbox()
        info = await sandbox.process.exec(command="echo hi", name="job")
        assert _body(api.requests[0]) == {"command": "echo hi", "name": "job"}
        assert info.stdout == "hi\n"
        assert (await sandbox.process.get(identifier="job")).pid == "42"

    @pytest.mark.asyncio
    async def test_exec_streams_output_then_exit(self) -> None:
        api = _Api()
        api.route(
            "POST",
            "/process",
            _ndjson(
                {"type": "stdout", "data": "hi\n"},
                {"type": "result", "data": json.dumps(_process())},
            ),
        )
        async with await api.async_sandbox().process.exec_stream(
            command="echo hi"
        ) as stream:
            events = [event async for event in stream]
        assert events[0] == SandboxProcessExecEventOutput(stream="stdout", text="hi\n")
        assert isinstance(events[1], SandboxProcessExecEventExit)

    @pytest.mark.asyncio
    async def test_streams_logs_by_line(self) -> None:
        api = _Api()
        api.route("GET", "/process/job/logs/stream", _text("stdout:one\n[keepalive]\n"))
        stream = await api.async_sandbox().process.stream_logs(identifier="job")
        assert [line async for line in stream] == [
            SandboxProcessLogLine(stream="stdout", text="one")
        ]

    @pytest.mark.asyncio
    async def test_waits_until_finished(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/process/job",
            _process(status="running"),
            _error(429),
            _process(status="stopped"),
        )
        info = await api.async_sandbox().process.wait(
            identifier="job", poll_interval=timedelta(milliseconds=1)
        )
        assert info.status == "stopped"

    @pytest.mark.asyncio
    async def test_wait_times_out_with_last_error_as_cause(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _process(status="running"), _RESET)
        with pytest.raises(SandboxProcessWaitTimeoutError) as raised:
            await api.async_sandbox().process.wait(
                identifier="job",
                timeout=timedelta(milliseconds=20),
                poll_interval=timedelta(milliseconds=1),
            )
        assert raised.value.identifier == "job"
        assert isinstance(raised.value.__cause__, httpx.RemoteProtocolError)

    @pytest.mark.asyncio
    async def test_wait_leaves_callers_own_timeout_alone(self) -> None:
        api = _Api()
        api.route("GET", "/process/job", _process(status="running"))
        sandbox = api.async_sandbox()
        with pytest.raises(TimeoutError) as raised:
            async with asyncio.timeout(0.02):
                await sandbox.process.wait(
                    identifier="job",
                    timeout=None,
                    poll_interval=timedelta(milliseconds=1),
                )
        assert not isinstance(raised.value, SandboxProcessWaitTimeoutError)

    @pytest.mark.asyncio
    async def test_writes_and_closes_stdin(self) -> None:
        api = _Api()
        api.route("POST", "/process/job/stdin", {"message": "ok"})
        api.route("DELETE", "/process/job/stdin", {"message": "ok"})
        sandbox = api.async_sandbox()
        await sandbox.process.write_stdin(identifier="job", data="x")
        await sandbox.process.close_stdin(identifier="job")
        assert [request.method for request in api.requests] == ["POST", "DELETE"]

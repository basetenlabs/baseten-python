from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import struct
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import h2.config
import h2.connection
import h2.errors
import h2.events
import httpx
import pytest

import baseten.sandbox._retry
from baseten.sandbox import SandboxAPIError, SandboxGatewayError, SandboxRetryOptions
from baseten.sandbox._retry import (
    is_transient_reset_error,
    resolve_retry_options,
    retry_idempotent,
    retry_idempotent_async,
)

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def short_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries take milliseconds instead of seconds.
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 0.001)
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 0.002)


def test_resolve_retry_options() -> None:
    resolved = resolve_retry_options(
        SandboxRetryOptions(gateway_max_retries=0, upload_max_retries=7)
    )
    assert resolved.read_max_retries == 5
    assert resolved.gateway_max_retries == 0
    assert resolved.upload_max_retries == 7


# Each case produces its failure on a real connection, since the errors that
# mark a dropped connection differ by platform, protocol, and sync or async
# I/O.
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
class TestIsTransientResetError:
    def test_closed_before_response(self, use_async: bool) -> None:
        with _http1_server(lambda conn: None) as url:
            err = _request_error(url, use_async=use_async)
        assert isinstance(err, httpx.RemoteProtocolError)
        assert is_transient_reset_error(err), repr(err)

    def test_closed_mid_body(self, use_async: bool) -> None:
        def respond_partially(conn: socket.socket) -> None:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")

        with _http1_server(respond_partially) as url:
            err = _request_error(url, use_async=use_async)
        assert isinstance(err, httpx.RemoteProtocolError)
        assert is_transient_reset_error(err), repr(err)

    def test_connection_reset(self, use_async: bool) -> None:
        def reset(conn: socket.socket) -> None:
            # Closing with no linger sends a reset instead of a clean close.
            linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)

        with _http1_server(reset) as url:
            err = _request_error(url, use_async=use_async)
        assert is_transient_reset_error(err), repr(err)

    def test_connection_refused(self, use_async: bool) -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        err = _request_error(f"http://127.0.0.1:{port}", use_async=use_async)
        assert isinstance(err, httpx.ConnectError)
        assert is_transient_reset_error(err), repr(err)

    def test_http2_stream_reset(self, use_async: bool) -> None:
        with _http2_server("reset") as url:
            err = _request_error(url, use_async=use_async, http2=True)
        assert "StreamReset" in str(err)
        assert is_transient_reset_error(err), repr(err)

    def test_http2_goaway(self, use_async: bool) -> None:
        with _http2_server("goaway") as url:
            err = _request_error(url, use_async=use_async, http2=True)
        assert "ConnectionTerminated" in str(err)
        assert is_transient_reset_error(err), repr(err)

    def test_http2_connection_dropped(self, use_async: bool) -> None:
        with _http2_server("drop") as url:
            err = _request_error(url, use_async=use_async, http2=True)
        assert is_transient_reset_error(err), repr(err)

    def test_read_timeout_never_transient(self, use_async: bool) -> None:
        # A timeout the caller configured is theirs to handle, not a dropped
        # connection.
        responded = threading.Event()
        with _http1_server(lambda conn: responded.wait()) as url:
            try:
                err = _request_error(
                    url, use_async=use_async, timeout=httpx.Timeout(5.0, read=0.05)
                )
            finally:
                responded.set()
        assert isinstance(err, httpx.ReadTimeout)
        assert not is_transient_reset_error(err), repr(err)


def test_api_error_never_transient() -> None:
    # A response came back, even when its body reads like a reset.
    err = SandboxAPIError(
        status=500,
        code="StreamReset",
        message="Server disconnected",
        details=None,
        body="ConnectionTerminated",
    )
    err.__cause__ = ConnectionResetError()
    assert not is_transient_reset_error(err)


def test_non_reset_failures_never_transient() -> None:
    assert not is_transient_reset_error(ValueError("bad value"))
    # A malformed response is a protocol error, but not a dropped connection.
    assert not is_transient_reset_error(httpx.RemoteProtocolError("illegal header"))
    # A name that does not resolve fails to connect, but will not on a retry.
    dns_failure = httpx.ConnectError("no such host")
    dns_failure.__cause__ = socket.gaierror(socket.EAI_NONAME, "no such host")
    assert not is_transient_reset_error(dns_failure)


def test_backoff_delay_doubles_up_to_max_plus_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 0.2)
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 2.0)
    for attempt, capped in [(1, 0.2), (2, 0.4), (4, 1.6), (5, 2.0), (9, 2.0)]:
        delay = baseten.sandbox._retry.backoff_delay(attempt)
        assert capped <= delay <= capped + 0.2


class TestRetryIdempotent:
    def test_retries_transient_up_to_limit(self) -> None:
        calls = 0

        def call() -> int:
            nonlocal calls
            calls += 1
            raise httpx.ReadError("reset") from ConnectionResetError()

        with pytest.raises(httpx.ReadError):
            retry_idempotent(call, max_retries=2, gateway_max_retries=0)
        assert calls == 3

    def test_succeeds_after_transient(self) -> None:
        calls = 0

        def call() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadError("reset") from ConnectionResetError()
            return 42

        assert retry_idempotent(call, max_retries=2, gateway_max_retries=0) == 42

    def test_gateway_has_own_budget(self) -> None:
        calls = 0

        def call() -> int:
            nonlocal calls
            calls += 1
            raise _gateway_error()

        with pytest.raises(SandboxGatewayError):
            retry_idempotent(call, max_retries=0, gateway_max_retries=1)
        assert calls == 2

    def test_non_transient_not_retried(self) -> None:
        calls = 0

        def call() -> int:
            nonlocal calls
            calls += 1
            raise SandboxAPIError(
                status=404, code=None, message=None, details=None, body=""
            )

        with pytest.raises(SandboxAPIError):
            retry_idempotent(call, max_retries=5, gateway_max_retries=5)
        assert calls == 1


class TestRetryIdempotentAsync:
    @pytest.mark.asyncio
    async def test_retries_transient_up_to_limit(self) -> None:
        calls = 0

        async def call() -> int:
            nonlocal calls
            calls += 1
            raise httpx.ReadError("reset") from ConnectionResetError()

        with pytest.raises(httpx.ReadError):
            await retry_idempotent_async(call, max_retries=2, gateway_max_retries=0)
        assert calls == 3

    @pytest.mark.asyncio
    async def test_gateway_has_own_budget(self) -> None:
        calls = 0

        async def call() -> int:
            nonlocal calls
            calls += 1
            raise _gateway_error()

        with pytest.raises(SandboxGatewayError):
            await retry_idempotent_async(call, max_retries=0, gateway_max_retries=1)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_cancellation_stops_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A backoff far longer than the test, so cancellation ends the wait.
        monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 3600.0)
        monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 3600.0)
        failed = asyncio.Event()

        async def call() -> int:
            failed.set()
            raise httpx.ReadError("reset") from ConnectionResetError()

        task = asyncio.create_task(
            retry_idempotent_async(call, max_retries=5, gateway_max_retries=0)
        )
        await failed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_cancellation_not_retried(self) -> None:
        calls = 0

        async def call() -> int:
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await retry_idempotent_async(call, max_retries=5, gateway_max_retries=5)
        assert calls == 1


def _gateway_error() -> SandboxGatewayError:
    return SandboxGatewayError(
        status=503, code=None, message=None, details=None, body=""
    )


def _request_error(url: str, *, use_async: bool, **client_options: Any) -> Exception:
    """Send a GET that must fail and return its error."""
    if "http2" in client_options:
        client_options["verify"] = ssl.create_default_context(
            cafile=str(_FIXTURES / "localhost-cert.pem")
        )
    if use_async:

        async def send() -> None:
            async with httpx.AsyncClient(**client_options) as client:
                await client.get(url)

        with pytest.raises(Exception) as info:
            asyncio.run(send())
    else:
        with pytest.raises(Exception) as info, httpx.Client(**client_options) as client:
            client.get(url)
    return info.value


@contextlib.contextmanager
def _http1_server(on_request: Callable[[socket.socket], object]) -> Iterator[str]:
    """Serve one HTTP/1.1 connection, handing it to ``on_request`` once the
    request has arrived, then closing it."""
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                request += chunk
            on_request(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        thread.join()
        listener.close()


@contextlib.contextmanager
def _http2_server(action: Literal["reset", "goaway", "drop"]) -> Iterator[str]:
    """Serve one HTTP/2 connection over TLS, failing its first request.

    ``reset`` resets the request's stream, ``goaway`` closes the connection
    with GOAWAY, and ``drop`` closes the socket without either.
    """
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(
        _FIXTURES / "localhost-cert.pem", _FIXTURES / "localhost-key.pem"
    )
    context.set_alpn_protocols(["h2"])
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        raw, _ = listener.accept()
        with context.wrap_socket(raw, server_side=True) as sock:
            conn = h2.connection.H2Connection(
                config=h2.config.H2Configuration(client_side=False)
            )
            conn.initiate_connection()
            sock.sendall(conn.data_to_send())
            # Serves until the client goes away, however that surfaces here.
            with contextlib.suppress(OSError):
                while data := sock.recv(65536):
                    for event in conn.receive_data(data):
                        if not isinstance(event, h2.events.RequestReceived):
                            continue
                        if action == "drop":
                            return
                        if action == "reset":
                            conn.reset_stream(
                                event.stream_id, h2.errors.ErrorCodes.INTERNAL_ERROR
                            )
                        else:
                            # Nothing is read after GOAWAY, since the
                            # connection accepts no more frames.
                            conn.close_connection()
                            sock.sendall(conn.data_to_send())
                            return
                    sock.sendall(conn.data_to_send())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        thread.join()
        listener.close()

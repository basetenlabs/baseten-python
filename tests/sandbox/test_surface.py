from __future__ import annotations

import json
import types
from typing import Any

import httpx
import pytest

import baseten.sandbox._auth
from baseten.sandbox import (
    AsyncSandboxClient,
    SandboxApiError,
    SandboxClient,
    SandboxGatewayError,
    SandboxInfo,
)

MANAGEMENT_URL = "https://api.baseten.co"
SANDBOX_URL = "https://sb-1.b10.co"
FAR_FUTURE = "2100-01-01T00:00:00Z"


def sandbox_record(name: str = "sb-1", status: str = "DEPLOYED") -> dict[str, Any]:
    return {
        "name": name,
        "url": SANDBOX_URL,
        "status": status,
        "state": "RUNNING",
        "created_at": "2026-09-29T00:00:00Z",
        "enabled": True,
    }


def process_record() -> dict[str, Any]:
    return {
        "command": "echo hi",
        "completedAt": "Tue, 30 Sep 2026 00:00:01 GMT",
        "exitCode": 0,
        "logs": "hi\n",
        "name": "echo",
        "pid": "123",
        "startedAt": "2026-09-30T00:00:00Z",
        "status": "completed",
        "stderr": "",
        "stdout": "hi\n",
        "workingDir": "/tmp",
    }


def file_record() -> dict[str, Any]:
    return {
        "content": "file text",
        "group": "root",
        "lastModified": "2026-09-29T00:00:00Z",
        "name": "file.txt",
        "owner": "root",
        "path": "/file.txt",
        "permissions": "0644",
        "size": 9,
    }


class RoutingTransport:
    """Mock transport routing by path, with scripted per-path responses.

    Each route maps path to either a response factory or a list consumed one
    entry per matching request.
    """

    def __init__(
        self,
        routes: dict[str, Any],
        mint_tokens: list[str] | None = None,
    ) -> None:
        self.routes = routes
        self.mint_tokens = mint_tokens or ["tok-1"]
        self.requests: list[tuple[str, str, str | None, bytes]] = []
        self.mint_count = 0

    def _respond(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        authorization = request.headers.get("authorization")
        self.requests.append((method, path, authorization, request.content))

        def json_response(payload: Any, status: int = 200) -> httpx.Response:
            return httpx.Response(
                status, json=payload, headers={"content-type": "application/json"}
            )

        if path == "/v1/token":
            token = self.mint_tokens[min(self.mint_count, len(self.mint_tokens) - 1)]
            self.mint_count += 1
            return json_response({"token": token, "expires_at": FAR_FUTURE})

        route = self.routes.get(f"{method} {path}")
        if route is None:
            route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, text=f"no route for {method} {path}")
        if isinstance(route, list):
            if not route:
                return httpx.Response(500, text="route exhausted")
            route = route.pop(0)
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(request)
        if isinstance(route, httpx.Response):
            return route
        return json_response(route)

    @property
    def sync_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._respond)

    @property
    def async_transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            return self._respond(request)

        return httpx.MockTransport(handler)

    def authorizations(self, path: str) -> list[str | None]:
        return [auth for _, p, auth, _ in self.requests if p == path and auth]

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _, _ in self.requests if m == method and p == path)


def make_sync_client(transport: RoutingTransport) -> SandboxClient:
    return SandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.sync_transport,
    )


def created_response(record: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        201,
        json=record or sandbox_record(),
        headers={"content-type": "application/json"},
    )


def revoked_token_response(token: str) -> httpx.Response:
    return httpx.Response(
        401,
        json={"code": "TOKEN_REVOKED", "message": "revoked"},
        headers={
            "content-type": "application/json",
            "x-blaxel-error-code": "TOKEN_REVOKED",
        },
    )


def test_create_mints_token_and_returns_sandbox() -> None:
    transport = RoutingTransport(
        routes={
            "POST /v1/sandboxes/instances": created_response(),
        }
    )
    client = make_sync_client(transport)

    sandbox = client.create(name="sb-1")

    assert sandbox.name == "sb-1"
    assert sandbox.url == SANDBOX_URL
    assert isinstance(sandbox.info, SandboxInfo)
    assert sandbox.info.status == "DEPLOYED"
    body = json.loads(transport.requests[1][3])
    assert body == {"name": "sb-1"}
    assert transport.authorizations("/v1/sandboxes/instances") == ["Bearer tok-1"]
    client.close()


def test_token_is_minted_once_and_cached() -> None:
    transport = RoutingTransport(
        routes={
            "POST /v1/sandboxes/instances": [created_response(), created_response()],
            "GET /v1/sandboxes/instances/sb-1": sandbox_record(),
        }
    )
    client = make_sync_client(transport)

    client.create()
    client.get_info("sb-1")

    assert transport.mint_count == 1
    client.close()


def test_empty_api_key_sends_no_authorization() -> None:
    transport = RoutingTransport(
        routes={"GET /v1/sandboxes/instances/sb-1": sandbox_record()}
    )
    client = SandboxClient(
        api_key="",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.sync_transport,
    )

    client.get_info("sb-1")

    assert transport.authorizations("/v1/sandboxes/instances/sb-1") == []
    client.close()


def test_revoked_token_is_replaced_and_request_resent() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances/sb-1": lambda request: (
                revoked_token_response("tok-1")
                if request.headers.get("authorization") == "Bearer tok-1"
                else httpx.Response(
                    200,
                    json=sandbox_record(),
                    headers={"content-type": "application/json"},
                )
            ),
        },
        mint_tokens=["tok-1", "tok-2"],
    )
    client = make_sync_client(transport)

    info = client.get_info("sb-1")

    assert info.name == "sb-1"
    assert transport.mint_count == 2
    assert transport.authorizations("/v1/sandboxes/instances/sb-1") == [
        "Bearer tok-1",
        "Bearer tok-2",
    ]
    client.close()


def test_list_follows_cursor_pages() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances": [
                {
                    "items": [sandbox_record("sb-1")],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {
                    "items": [sandbox_record("sb-2")],
                    "pagination": {"has_more": False},
                },
            ],
        }
    )
    client = make_sync_client(transport)

    names = [info.name for info in client.list()]

    assert names == ["sb-1", "sb-2"]
    assert transport.count("GET", "/v1/sandboxes/instances") == 2
    client.close()


def test_list_repeated_cursor_raises() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances": {
                "items": [sandbox_record()],
                "pagination": {"has_more": True, "cursor": "c1"},
            },
        }
    )
    client = make_sync_client(transport)

    with pytest.raises(ValueError, match="repeated cursor"):
        list(client.list())

    client.close()


def test_delete_returns_record() -> None:
    transport = RoutingTransport(
        routes={
            "DELETE /v1/sandboxes/instances/sb-1": httpx.Response(
                202,
                json=sandbox_record(status="DELETING"),
                headers={"content-type": "application/json"},
            )
        }
    )
    client = make_sync_client(transport)

    info = client.delete("sb-1")

    assert info.status == "DELETING"
    client.close()


def test_control_plane_error_becomes_sandbox_api_error() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances/sb-1": httpx.Response(
                404, json={"code": "NOT_FOUND", "message": "no such sandbox"}
            )
        }
    )
    client = make_sync_client(transport)

    with pytest.raises(SandboxApiError) as exc_info:
        client.get_info("sb-1")

    assert exc_info.value.status == 404
    assert exc_info.value.code == "NOT_FOUND"
    client.close()


def make_full_client(transport: RoutingTransport) -> SandboxClient:
    transport.routes.setdefault("POST /v1/sandboxes/instances", created_response())
    transport.routes.setdefault("GET /filesystem/file.txt", file_record())
    transport.routes.setdefault("PUT /filesystem/file.txt", {"message": "written"})
    transport.routes.setdefault("POST /process", process_record())
    return make_sync_client(transport)


def test_fs_read_and_write_on_created_sandbox() -> None:
    transport = RoutingTransport(routes={})
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    assert sandbox.fs.read("file.txt") == "file text"
    sandbox.fs.write("file.txt", "new text")

    assert transport.count("GET", "/filesystem/file.txt") == 1
    written = json.loads(transport.requests[-1][3])
    assert written == {"content": "new text"}
    client.close()


def test_fs_read_retries_dropped_connection() -> None:
    transport = RoutingTransport(
        routes={
            "GET /filesystem/file.txt": [httpx.ConnectError("dropped"), file_record()]
        }
    )
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    content = sandbox.fs.read("file.txt")

    assert content == "file text"
    assert transport.count("GET", "/filesystem/file.txt") == 2
    client.close()


@pytest.mark.asyncio
async def test_async_fs_read_and_write_retry_transient_failures() -> None:
    transport = RoutingTransport(
        routes={
            "POST /v1/sandboxes/instances": created_response(),
            "GET /filesystem/file.txt": [
                httpx.ConnectError("dropped"),
                httpx.Response(502, text="bad gateway"),
                file_record(),
            ],
            "PUT /filesystem/file.txt": [
                httpx.ConnectError("dropped"),
                httpx.Response(503, text="unavailable"),
                {"message": "written"},
            ],
        }
    )
    client = AsyncSandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.async_transport,
    )
    sandbox = await client.create(name="sb-1")

    content = await sandbox.fs.read("file.txt")
    await sandbox.fs.write("file.txt", content)

    assert content == "file text"
    assert transport.count("GET", "/filesystem/file.txt") == 3
    assert transport.count("PUT", "/filesystem/file.txt") == 3
    await client.close()


def test_streaming_body_is_sent_once_on_revocation() -> None:
    # A stream-backed body cannot be replayed, so a revoked token surfaces
    # instead of a re-send that would have to materialize the stream.
    sends: list[bytes] = []

    def revoked_route(request: httpx.Request) -> httpx.Response:
        sends.append(request.read())
        return revoked_token_response("revoked")

    transport = RoutingTransport(
        routes={"POST /filesystem/big.bin": revoked_route},
        mint_tokens=["tok-1"],
    )
    client = make_sync_client(transport)

    stream = httpx.Client(
        transport=client._auth_transport,
        base_url=MANAGEMENT_URL,
        headers=_request_headers_for_test(),
    )
    response = stream.post("/filesystem/big.bin", content=iter([b"part-1", b"part-2"]))

    assert response.status_code == 401
    assert sends == [b"part-1part-2"]
    assert transport.mint_count == 1
    client.close()


def _request_headers_for_test() -> dict[str, str]:
    return {"user-agent": "test"}


def test_error_only_body_carries_its_error_as_code_and_description() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances/sb-1": httpx.Response(
                409,
                json={"error": "SANDBOX_NAME_TAKEN"},
            )
        }
    )
    client = make_sync_client(transport)

    with pytest.raises(SandboxApiError) as exc_info:
        client.get_info("sb-1")

    assert exc_info.value.code == "SANDBOX_NAME_TAKEN"
    assert "SANDBOX_NAME_TAKEN" in str(exc_info.value)
    client.close()


def test_fs_read_directory_raises() -> None:
    transport = RoutingTransport(
        routes={
            "GET /filesystem/dir": {
                "files": [],
                "name": "dir",
                "path": "/dir",
                "subdirectories": [],
            }
        }
    )
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    with pytest.raises(ValueError, match="is a directory"):
        sandbox.fs.read("dir")

    client.close()


def test_process_list_and_logs() -> None:
    def process_list_response(request: httpx.Request) -> httpx.Response:
        # Callable route: a bare list payload would be consumed one entry per request.
        return httpx.Response(
            200, json=[process_record()], headers={"content-type": "application/json"}
        )

    transport = RoutingTransport(
        routes={
            "GET /process": process_list_response,
            "GET /process/123/logs": {"logs": "hi\n", "stdout": "hi\n", "stderr": ""},
        }
    )
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    processes = sandbox.process.list()
    logs = sandbox.process.logs("123")

    assert [process.pid for process in processes] == ["123"]
    assert processes[0].status == "completed"
    assert logs.logs == "hi\n"
    assert logs.stderr == ""
    client.close()


async def _async_sandbox(transport: RoutingTransport):
    client = AsyncSandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.async_transport,
    )
    return client, await client.create(name="sb-1")


@pytest.mark.asyncio
async def test_async_process_list_and_logs() -> None:
    def process_list_response(request: httpx.Request) -> httpx.Response:
        # Callable route: a bare list payload would be consumed one entry per request.
        return httpx.Response(
            200, json=[process_record()], headers={"content-type": "application/json"}
        )

    transport = RoutingTransport(
        routes={
            "POST /v1/sandboxes/instances": created_response(),
            "GET /process": process_list_response,
            "GET /process/123/logs": {"logs": "hi\n", "stdout": "hi\n", "stderr": ""},
        }
    )
    client, sandbox = await _async_sandbox(transport)

    processes = await sandbox.process.list()
    logs = await sandbox.process.logs("123")

    assert [process.pid for process in processes] == ["123"]
    assert logs.logs == "hi\n"
    await client.close()


def test_process_exec_returns_curated_info() -> None:
    transport = RoutingTransport(routes={})
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    info = sandbox.process.exec("echo hi", working_dir="/tmp", wait_for_completion=True)

    assert info.pid == "123"
    assert info.status == "completed"
    assert info.exit_code == 0
    assert info.working_dir == "/tmp"
    assert info.completed_at is not None
    assert info.completed_at.year == 2026
    body = json.loads(transport.requests[-1][3])
    assert body["command"] == "echo hi"
    assert body["workingDir"] == "/tmp"
    assert body["waitForCompletion"] is True
    client.close()


def test_exec_gateway_error_is_sandbox_gateway_error() -> None:
    transport = RoutingTransport(
        routes={"POST /process": httpx.Response(502, text="bad gateway")}
    )
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    with pytest.raises(SandboxGatewayError) as exc_info:
        sandbox.process.exec("echo hi")

    assert exc_info.value.status == 502
    client.close()


def test_get_by_name_and_by_info() -> None:
    transport = RoutingTransport(
        routes={"GET /v1/sandboxes/instances/sb-1": sandbox_record()}
    )
    client = make_sync_client(transport)

    info = client.get_info("sb-1")
    by_name = client.get("sb-1")
    assert by_name.name == "sb-1"
    assert by_name.info is None

    by_record = client.get(info)
    assert by_record.name == "sb-1"

    # The by-record get made no call of its own.
    assert transport.count("GET", "/v1/sandboxes/instances/sb-1") == 2
    client.close()


@pytest.mark.asyncio
async def test_async_client_create_and_exec() -> None:
    transport = RoutingTransport(routes={})
    transport.routes.setdefault("POST /v1/sandboxes/instances", created_response())
    transport.routes.setdefault("POST /process", process_record())
    client = AsyncSandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.async_transport,
    )

    sandbox = await client.create(name="sb-1")
    info = await sandbox.process.exec("echo hi")

    assert info.pid == "123"
    assert transport.authorizations("/process") == ["Bearer tok-1"]
    await client.close()


@pytest.mark.asyncio
async def test_async_list_paginates() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances": [
                {
                    "items": [sandbox_record("sb-1")],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {
                    "items": [sandbox_record("sb-2")],
                    "pagination": {"has_more": False},
                },
            ],
        }
    )
    client = AsyncSandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.async_transport,
    )

    names = [info.name async for info in client.list()]

    assert names == ["sb-1", "sb-2"]
    await client.close()


def test_create_without_name_omits_the_field() -> None:
    transport = RoutingTransport(
        routes={"POST /v1/sandboxes/instances": created_response()}
    )
    client = make_sync_client(transport)

    client.create()

    body = json.loads(transport.requests[1][3])
    assert body == {}
    client.close()


def test_fs_read_retries_gateway_errors() -> None:
    transport = RoutingTransport(
        routes={
            "GET /filesystem/file.txt": [
                httpx.Response(502, text="bad gateway"),
                httpx.Response(503, text="unavailable"),
                file_record(),
            ]
        }
    )
    client = make_full_client(transport)

    sandbox = client.create(name="sb-1")
    content = sandbox.fs.read("file.txt")

    assert content == "file text"
    assert transport.count("GET", "/filesystem/file.txt") == 3
    client.close()


def test_persistent_revocation_returns_last_response(monkeypatch) -> None:
    # The first resend is immediate; later ones wait out the revocation's
    # rounded-up cutoff second, here shortened to keep the test fast.
    delays: list[float] = []
    monkeypatch.setattr(
        baseten.sandbox._auth, "_TOKEN_REVOKED_RETRY_DELAY_SECONDS", 0.01
    )
    monkeypatch.setattr(
        baseten.sandbox._auth,
        "time",
        types.SimpleNamespace(sleep=lambda s: delays.append(s)),
    )
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances/sb-1": lambda request: revoked_token_response(
                "revoked"
            )
        },
        mint_tokens=["tok-1", "tok-2", "tok-3"],
    )
    client = make_sync_client(transport)

    with pytest.raises(SandboxApiError) as exc_info:
        client.get_info("sb-1")

    # Exhaustion surfaces the final 401 as a converted error, and every
    # minted token was tried, the later resends past the cutoff.
    assert exc_info.value.status == 401
    assert transport.mint_count == 3
    assert delays == [0.01]
    client.close()


def test_get_info_show_secrets_reaches_the_query() -> None:
    seen_queries: list[str] = []

    def show_secrets_route(request: httpx.Request) -> httpx.Response:
        seen_queries.append(str(request.url.params))
        return httpx.Response(
            200, json=sandbox_record(), headers={"content-type": "application/json"}
        )

    transport = RoutingTransport(
        routes={"GET /v1/sandboxes/instances/sb-1": show_secrets_route}
    )
    client = make_sync_client(transport)

    client.get_info("sb-1")
    client.get_info("sb-1", show_secrets=True)

    assert "show_secrets=false" in seen_queries[0]
    assert "show_secrets=true" in seen_queries[1]
    client.close()


def test_sandbox_from_url_makes_no_call_and_shares_auth() -> None:
    transport = RoutingTransport(
        routes={
            "POST /process": process_record(),
        }
    )
    client = make_sync_client(transport)

    sandbox = client.sandbox_from_url("https://sb-1.invalid")
    assert sandbox.name == ""
    assert sandbox.url == "https://sb-1.invalid"
    # No control-plane call was made to resolve it.
    assert transport.count("GET", "/v1/sandboxes/instances/sb-1") == 0

    # The sandbox shares the client's token: its exec call carries it.
    sandbox.process.exec("echo hi", wait_for_completion=True)
    assert transport.authorizations("/process") == ["Bearer tok-1"]
    client.close()


def test_typed_error_body_becomes_a_dict() -> None:
    transport = RoutingTransport(
        routes={
            "GET /v1/sandboxes/instances/sb-1": httpx.Response(
                404,
                json={"code": "NOT_FOUND", "message": "no such sandbox"},
            )
        }
    )
    client = make_sync_client(transport)

    with pytest.raises(SandboxApiError) as exc_info:
        client.get_info("sb-1")

    assert exc_info.value.code == "NOT_FOUND"
    assert isinstance(exc_info.value.body, dict)
    client.close()


def library_catalog_response(request: httpx.Request) -> httpx.Response:
    # Callable route: a dict payload would be consumed as a scripted response.
    return httpx.Response(
        200,
        json={"items": library_catalog()},
        headers={"content-type": "application/json"},
    )


def library_catalog() -> list[dict[str, Any]]:
    # The server drops hidden and coming-soon entries before listing.
    return [
        {
            "name": "expo",
            "image": "baseten/expo:latest",
            "display_name": "Expo",
            "description": "Expo app development",
            "long_description": "Build and run Expo apps.",
            "memory": 4096,
            "categories": ["web"],
            "tags": ["react-native"],
            "ports": [{"name": "http", "target": 3000, "protocol": "HTTP"}],
            "icon": "https://images.b10.co/expo.svg",
            "url": "https://expo.dev",
            "enterprise": False,
        }
    ]


def test_library_images_translates_the_served_catalog() -> None:
    transport = RoutingTransport(
        routes={"GET /v1/sandboxes/library_images": library_catalog_response}
    )
    client = make_sync_client(transport)

    images = client.library_images()

    assert [image.name for image in images] == ["expo"]
    image = images[0]
    assert image.image == "baseten/expo:latest"
    assert image.display_name == "Expo"
    assert image.memory == 4096
    assert image.categories == ["web"]
    assert image.ports[0].target == 3000
    assert image.icon_url == "https://images.b10.co/expo.svg"
    assert image.project_url == "https://expo.dev"
    client.close()


@pytest.mark.asyncio
async def test_async_library_images_translates() -> None:
    transport = RoutingTransport(
        routes={"GET /v1/sandboxes/library_images": library_catalog_response}
    )
    client = AsyncSandboxClient(
        api_key="test-key",
        base_url_override=MANAGEMENT_URL,
        transport_override=transport.async_transport,
    )

    images = await client.library_images()

    assert [image.name for image in images] == ["expo"]
    await client.close()

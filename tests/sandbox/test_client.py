from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import baseten.sandbox._client
from baseten.sandbox import (
    AsyncSandboxClient,
    SandboxAPIError,
    SandboxClient,
    SandboxEnvValue,
    SandboxError,
    SandboxExpirationPolicyIdle,
    SandboxInfo,
    SandboxLifecycle,
    SandboxNetwork,
    SandboxPort,
)

_SANDBOX_URL = "https://sbx.example.dev/"


class _Api:
    """Fake control plane, token exchange, and sandbox, recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.authorizations: list[str | None] = []
        self.mints = 0
        self.routes: dict[
            tuple[str, str], Callable[[httpx.Request], httpx.Response]
        ] = {}

    def route(
        self,
        method: str,
        path: str,
        respond: Callable[[httpx.Request], httpx.Response]
        | httpx.Response
        | dict[str, Any],
        *,
        status: int = 200,
    ) -> None:
        if isinstance(respond, dict):
            body = respond
            self.routes[(method, path)] = lambda request: httpx.Response(
                status, json=body
            )
        elif isinstance(respond, httpx.Response):
            # A response is single use, so each request gets a copy.
            template = respond
            self.routes[(method, path)] = lambda request: httpx.Response(
                template.status_code,
                headers=template.headers,
                content=template.content,
            )
        else:
            self.routes[(method, path)] = respond

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            self.mints += 1
            return httpx.Response(
                200,
                json={
                    "token": f"token-{self.mints}",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                },
            )
        self.requests.append(request)
        self.authorizations.append(request.headers.get("Authorization"))
        return self.routes[(request.method, request.url.path)](request)

    def client(self, **options: Any) -> SandboxClient:
        return SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
            **options,
        )

    def async_client(self, **options: Any) -> AsyncSandboxClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            return self.handle(request)

        return AsyncSandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
            **options,
        )


def _record(name: str = "sbx", **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "url": _SANDBOX_URL,
        "status": "DEPLOYED",
        "created_at": "2026-10-06T12:00:00Z",
        **fields,
    }


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


class TestSandboxClient:
    def test_creates_with_nothing_set_leaving_defaults_to_server(self) -> None:
        api = _Api()
        api.route("POST", "/v1/sandboxes/instances", _record(), status=201)
        result = api.client().create()
        request = api.requests[0]
        assert str(request.url) == "https://api.test/v1/sandboxes/instances"
        assert _body(request) == {}
        assert result.info.name == "sbx"
        assert (result.name, result.url) == ("sbx", _SANDBOX_URL)

    def test_creates_with_every_field_mapped(self) -> None:
        api = _Api()
        api.route("POST", "/v1/sandboxes/instances", _record(), status=201)
        api.client().create(
            name="sbx",
            create_if_not_exists=True,
            image="img:1",
            memory=2048,
            region="us-was-1",
            envs={"A": SandboxEnvValue(value="1", secret=False)},
            labels={"team": "infra"},
            external_id="ext-1",
            lifecycle=SandboxLifecycle(
                expiration_policies=[
                    SandboxExpirationPolicyIdle(after=timedelta(minutes=5))
                ]
            ),
            ports=[SandboxPort(target=80)],
            network=SandboxNetwork(subnet="default"),
        )
        assert _body(api.requests[0]) == {
            "name": "sbx",
            "create_if_not_exists": True,
            "image": "img:1",
            "memory": 2048,
            "region": "us-was-1",
            "envs": [{"name": "A", "value": "1", "secret": False}],
            "labels": {"team": "infra"},
            "external_id": "ext-1",
            "lifecycle": {
                "expiration_policies": [
                    {"type": "TTL_IDLE", "action": "DELETE", "value": "300000ms"}
                ]
            },
            "ports": [{"target": 80}],
            "network": {"subnet": "default"},
        }

    def test_sends_team_id_on_every_call_when_set(self) -> None:
        api = _Api()
        api.route("POST", "/v1/sandboxes/instances", _record(), status=201)
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        api.route(
            "GET",
            "/v1/sandboxes/instances",
            {"items": [], "pagination": {"has_more": False}},
        )
        api.route("PATCH", "/v1/sandboxes/instances/sbx", _record())
        api.route("DELETE", "/v1/sandboxes/instances/sbx", _record(), status=202)
        client = api.client(team_id="team-1")
        client.create()
        client.get_info(name="sbx")
        list(client.list())
        client.update(name="sbx", labels={"a": "b"})
        client.delete(name="sbx")
        assert [r.url.params.get("team_id") for r in api.requests] == ["team-1"] * 5

    def test_omits_team_id_when_unset(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        api.client().get_info(name="sbx")
        assert "team_id" not in api.requests[0].url.params

    def test_gets_info_with_show_secrets_only_when_set(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        client = api.client()
        client.get_info(name="sbx")
        client.get_info(name="sbx", show_secrets=True)
        assert [r.url.params.get("show_secrets") for r in api.requests] == [
            None,
            "true",
        ]

    def test_gets_sandbox_by_name_with_call_and_from_record_without(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        client = api.client()
        by_name = client.get(name="sbx")
        assert len(api.requests) == 1
        from_record = client.get(info=client.get_info(name="sbx"))
        assert len(api.requests) == 2
        assert (by_name.name, by_name.url) == ("sbx", _SANDBOX_URL)
        assert (from_record.name, from_record.url) == ("sbx", _SANDBOX_URL)

    def test_get_needs_exactly_one_of_name_or_info(self) -> None:
        client = _Api().client()
        with pytest.raises(TypeError, match="exactly one"):
            client.get()  # ty: ignore[no-matching-overload]
        info = _info()
        with pytest.raises(TypeError, match="exactly one"):
            client.get(name="sbx", info=info)  # ty: ignore[no-matching-overload]

    def test_builds_sandbox_from_url_without_call(self) -> None:
        api = _Api()
        sandbox = api.client().sandbox_from_url(url=_SANDBOX_URL)
        assert (sandbox.name, sandbox.url) == ("", _SANDBOX_URL)
        assert api.requests == []

    def test_gives_sandboxes_client_token(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        api.route("GET", "/process", httpx.Response(200, json=[]))
        client = api.client(headers={"X-Extra": "1"})
        client.get(name="sbx").raw_api.get_process()
        exec_request = api.requests[1]
        assert str(exec_request.url) == "https://sbx.example.dev/process"
        assert api.authorizations == ["Bearer token-1", "Bearer token-1"]
        assert api.mints == 1
        for request in api.requests:
            assert request.headers["X-Extra"] == "1"
            assert request.headers["User-Agent"].startswith("baseten-python/")

    def test_drops_client_token_when_sandbox_finds_it_revoked(self) -> None:
        api = _Api()
        revoked = {"done": False}

        def revoke_once(request: httpx.Request) -> httpx.Response:
            if revoked["done"]:
                return httpx.Response(200, json=[])
            revoked["done"] = True
            return httpx.Response(401, headers={"x-blaxel-error-code": "TOKEN_REVOKED"})

        api.route("GET", "/process", revoke_once)
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        client = api.client()
        client.sandbox_from_url(url=_SANDBOX_URL).raw_api.get_process()
        client.get_info(name="sbx")
        assert api.authorizations == [
            "Bearer token-1",
            "Bearer token-2",
            "Bearer token-2",
        ]

    def test_lists_lazily_across_pages_with_filters_on_each(self) -> None:
        api = _Api()
        pages = iter(
            [
                {
                    "items": [_record("a"), _record("b")],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {"items": [_record("c")], "pagination": {"has_more": False}},
            ]
        )
        api.route(
            "GET",
            "/v1/sandboxes/instances",
            lambda request: httpx.Response(200, json=next(pages)),
        )
        listed = api.client().list(
            query="q", statuses=["DEPLOYED", "FAILED"], page_size=2
        )
        assert next(listed).name == "a"
        assert len(api.requests) == 1
        assert [info.name for info in listed] == ["b", "c"]
        first, second = (request.url.params for request in api.requests)
        assert "cursor" not in first
        assert second["cursor"] == "c1"
        for params in (first, second):
            assert params.get_list("status") == ["DEPLOYED", "FAILED"]
            assert (params["q"], params["limit"]) == ("q", "2")

    def test_stops_listing_on_repeated_cursor(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/instances",
            {"items": [_record()], "pagination": {"has_more": True, "cursor": "same"}},
        )
        with pytest.raises(
            SandboxError, match="sandbox list returned a repeated cursor"
        ):
            list(api.client().list())
        assert len(api.requests) == 2

    def test_updates_with_every_field_mapped(self) -> None:
        api = _Api()
        api.route("PATCH", "/v1/sandboxes/instances/sbx", _record(labels={"rev": "2"}))
        info = api.client().update(
            name="sbx",
            lifecycle=SandboxLifecycle(terminated_retention=timedelta(hours=1)),
            envs={"A": SandboxEnvValue(value="1")},
            external_id="ext-2",
            labels={"rev": "2"},
        )
        assert _body(api.requests[0]) == {
            "lifecycle": {"terminated_retention": "3600000ms"},
            "envs": [{"name": "A", "value": "1"}],
            "external_id": "ext-2",
            "labels": {"rev": "2"},
        }
        assert info.labels == {"rev": "2"}

    def test_updates_only_fields_that_are_set(self) -> None:
        api = _Api()
        api.route("PATCH", "/v1/sandboxes/instances/sbx", _record())
        api.client().update(name="sbx", labels={})
        assert _body(api.requests[0]) == {"labels": {}}

    def test_fails_update_server_rejects(self) -> None:
        api = _Api()
        api.route(
            "PATCH",
            "/v1/sandboxes/instances/sbx",
            httpx.Response(
                400, content=b'{"error": "BAD_REQUEST", "message": "nothing to update"}'
            ),
        )
        with pytest.raises(SandboxAPIError) as info:
            api.client().update(name="sbx")
        assert (info.value.status, info.value.code, info.value.message) == (
            400,
            "BAD_REQUEST",
            "nothing to update",
        )

    def test_never_retries_control_plane(self) -> None:
        api = _Api()
        api.route(
            "GET", "/v1/sandboxes/instances/sbx", httpx.Response(503, content=b"")
        )
        with pytest.raises(SandboxAPIError):
            api.client().get_info(name="sbx")
        assert len(api.requests) == 1

    def test_deletes_and_returns_record(self) -> None:
        api = _Api()
        api.route(
            "DELETE",
            "/v1/sandboxes/instances/sbx",
            _record(status="DELETING"),
            status=202,
        )
        assert api.client().delete(name="sbx").status == "DELETING"

    def test_closes_only_its_own_http_client(self) -> None:
        own = SandboxClient(api_key="key", http2=False)
        own.close()
        assert own.http_client.is_closed
        override = httpx.Client()
        SandboxClient(api_key="key", http_client_override=override).close()
        assert not override.is_closed
        override.close()


class TestAsyncSandboxClient:
    @pytest.mark.asyncio
    async def test_creates_and_gets_sandbox(self) -> None:
        api = _Api()
        api.route("POST", "/v1/sandboxes/instances", _record(), status=201)
        api.route("GET", "/v1/sandboxes/instances/sbx", _record())
        api.route("GET", "/process", httpx.Response(200, json=[]))
        client = api.async_client(team_id="team-1")
        created = await client.create(name="sbx")
        assert created.info.name == "sbx"
        sandbox = await client.get(name="sbx")
        await sandbox.raw_api.get_process()
        assert _body(api.requests[0]) == {"name": "sbx"}
        assert [r.url.params.get("team_id") for r in api.requests[:2]] == ["team-1"] * 2
        assert api.authorizations == ["Bearer token-1"] * 3

    @pytest.mark.asyncio
    async def test_lists_across_pages(self) -> None:
        api = _Api()
        pages = iter(
            [
                {
                    "items": [_record("a")],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {"items": [_record("b")], "pagination": {"has_more": False}},
            ]
        )
        api.route(
            "GET",
            "/v1/sandboxes/instances",
            lambda request: httpx.Response(200, json=next(pages)),
        )
        names = [info.name async for info in api.async_client().list()]
        assert names == ["a", "b"]

    @pytest.mark.asyncio
    async def test_converts_error_responses(self) -> None:
        api = _Api()
        api.route(
            "DELETE",
            "/v1/sandboxes/instances/sbx",
            httpx.Response(404, content=b'{"error": "NOT_FOUND", "message": "gone"}'),
        )
        with pytest.raises(SandboxAPIError) as info:
            await api.async_client().delete(name="sbx")
        assert info.value.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_closes_only_its_own_http_client(self) -> None:
        own = AsyncSandboxClient(api_key="key", http2=False)
        await own.close()
        assert own.http_client.is_closed
        override = httpx.AsyncClient()
        await AsyncSandboxClient(api_key="key", http_client_override=override).close()
        assert not override.is_closed
        await override.aclose()


class TestUseHttp2:
    def test_uses_http2_when_h2_installed(self) -> None:
        assert baseten.sandbox._client._use_http2(None)
        assert baseten.sandbox._client._use_http2(True)
        assert not baseten.sandbox._client._use_http2(False)

    def test_without_h2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        assert not baseten.sandbox._client._use_http2(None)
        with pytest.raises(ImportError, match="httpx\\[http2\\]"):
            baseten.sandbox._client._use_http2(True)


def _info() -> SandboxInfo:
    return SandboxInfo(
        name="sbx",
        url=_SANDBOX_URL,
        status="DEPLOYED",
        image=None,
        memory=None,
        region=None,
        envs={},
        labels={},
        external_id=None,
        lifecycle=None,
        ports=[],
        network=None,
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
        updated_at=None,
        created_by=None,
        updated_by=None,
        last_used_at=None,
        expires_in=None,
    )

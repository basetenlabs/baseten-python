from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from baseten.sandbox import (
    AsyncSandbox,
    AsyncSandboxClient,
    Sandbox,
    SandboxAPIError,
    SandboxClient,
    SandboxPreviewInfo,
    SandboxPreviewTokenInfo,
)

_PREVIEWS = "/v1/sandboxes/instances/sbx/previews"

_PREVIEW = {
    "name": "web",
    "port": 3000,
    "url": "https://p.example",
    "status": "DEPLOYED",
    "public": False,
    "region": "us-was-1",
    "ttl": "7d",
    "expires_at": "2026-10-20T12:00:00Z",
    "response_headers": {"A": "1"},
    "request_headers": {"Authorization": "****"},
    "created_at": "2026-10-13T12:00:00Z",
    "created_by": "me",
}

_TOKEN = {
    "name": "t1",
    "token": "secret",
    "expires_at": "2026-10-14T12:00:00Z",
    "expired": False,
}


class _Api:
    """Fake control plane and token exchange, recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[
            tuple[str, str], Callable[[httpx.Request], httpx.Response]
        ] = {}

    def route(self, method: str, path: str, body: Any, *, status: int = 200) -> None:
        self.routes[(method, path)] = lambda request: httpx.Response(status, json=body)

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
        return self.routes[(request.method, request.url.path)](request)

    def client(self) -> SandboxClient:
        return SandboxClient(
            api_key="key",
            team_id="team-1",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
        )

    def sandbox(self) -> Sandbox:
        return self.client().sandbox_from_url(url="https://sbx.example")

    def named_sandbox(self) -> Sandbox:
        self.route(
            "GET",
            "/v1/sandboxes/instances/sbx",
            {
                "name": "sbx",
                "url": "https://sbx.example",
                "status": "DEPLOYED",
                "created_at": "2026-10-13T12:00:00Z",
            },
        )
        sandbox = self.client().get(name="sbx")
        self.requests.clear()
        return sandbox

    async def async_named_sandbox(self) -> AsyncSandbox:
        self.route(
            "GET",
            "/v1/sandboxes/instances/sbx",
            {
                "name": "sbx",
                "url": "https://sbx.example",
                "status": "DEPLOYED",
                "created_at": "2026-10-13T12:00:00Z",
            },
        )

        async def handle(request: httpx.Request) -> httpx.Response:
            return self.handle(request)

        client = AsyncSandboxClient(
            api_key="key",
            team_id="team-1",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )
        sandbox = await client.get(name="sbx")
        self.requests.clear()
        return sandbox


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


class TestSandboxPreviews:
    def test_creates_sending_only_set_fields_and_maps_record(self) -> None:
        api = _Api()
        api.route("POST", _PREVIEWS, _PREVIEW, status=201)
        info = api.named_sandbox().previews.create(
            port=3000, name="web", response_headers={}, ttl=timedelta(minutes=90)
        )
        request = api.requests[0]
        assert request.url.params["team_id"] == "team-1"
        assert _body(request) == {
            "port": 3000,
            "name": "web",
            "response_headers": {},
            "ttl": "5400000ms",
        }
        assert info == SandboxPreviewInfo(
            name="web",
            port=3000,
            url="https://p.example/",
            status="DEPLOYED",
            public=False,
            response_headers={"A": "1"},
            request_headers={"Authorization": "****"},
            ttl=timedelta(days=7),
            expires_at=datetime(2026, 10, 20, 12, tzinfo=UTC),
            region="us-was-1",
            created_at=datetime(2026, 10, 13, 12, tzinfo=UTC),
            updated_at=None,
            created_by="me",
            updated_by=None,
        )

    def test_creates_with_every_field(self) -> None:
        api = _Api()
        api.route("POST", _PREVIEWS, _PREVIEW, status=201)
        api.named_sandbox().previews.create(
            port=3000,
            create_if_not_exists=True,
            public=True,
            request_headers={"Authorization": "Bearer x"},
            expires_at=datetime(2026, 10, 20, 12, tzinfo=UTC),
        )
        assert _body(api.requests[0]) == {
            "port": 3000,
            "create_if_not_exists": True,
            "public": True,
            "request_headers": {"Authorization": "Bearer x"},
            "expires_at": "2026-10-20T12:00:00Z",
        }

    def test_raises_api_errors(self) -> None:
        api = _Api()
        api.route(
            "POST", _PREVIEWS, {"code": "CONFLICT", "message": "exists"}, status=409
        )
        with pytest.raises(SandboxAPIError) as raised:
            api.named_sandbox().previews.create(port=3000)
        assert raised.value.code == "CONFLICT"

    def test_gets_info_with_show_secrets(self) -> None:
        api = _Api()
        api.route("GET", f"{_PREVIEWS}/web", _PREVIEW)
        info = api.named_sandbox().previews.get_info(name="web", show_secrets=True)
        assert info.name == "web"
        assert api.requests[0].url.params["show_secrets"] == "true"

    def test_lists_across_pages(self) -> None:
        api = _Api()
        pages = iter(
            [
                {
                    "items": [{**_PREVIEW, "name": "a"}],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {
                    "items": [{**_PREVIEW, "name": "b"}],
                    "pagination": {"has_more": False},
                },
            ]
        )
        api.routes[("GET", _PREVIEWS)] = lambda request: httpx.Response(
            200, json=next(pages)
        )
        names = [info.name for info in api.named_sandbox().previews.list(page_size=1)]
        assert names == ["a", "b"]
        assert [r.url.params.get("cursor") for r in api.requests] == [None, "c1"]
        assert api.requests[0].url.params["limit"] == "1"

    def test_updates_sending_only_set_fields(self) -> None:
        api = _Api()
        api.route("PATCH", f"{_PREVIEWS}/web", _PREVIEW)
        previews = api.named_sandbox().previews
        previews.update(name="web", public=False, request_headers={})
        previews.update(name="web")
        # False is sent, since it differs from unchanged, and an empty mapping
        # clears the set; the generated model's default false is never sent.
        assert _body(api.requests[0]) == {"public": False, "request_headers": {}}
        assert _body(api.requests[1]) == {}

    def test_deletes_returning_record(self) -> None:
        api = _Api()
        api.route("DELETE", f"{_PREVIEWS}/web", {**_PREVIEW, "status": "DELETING"})
        assert api.named_sandbox().previews.delete(name="web").status == "DELETING"

    def test_creates_lists_and_deletes_tokens(self) -> None:
        api = _Api()
        api.route("POST", f"{_PREVIEWS}/web/tokens", _TOKEN, status=201)
        api.route(
            "GET",
            f"{_PREVIEWS}/web/tokens",
            {"items": [_TOKEN], "pagination": {"has_more": False}},
        )
        api.routes[("DELETE", f"{_PREVIEWS}/web/tokens/t1")] = lambda request: (
            httpx.Response(204)
        )
        previews = api.named_sandbox().previews
        token = previews.create_token(
            preview_name="web",
            expires_at=datetime(2026, 10, 14, 12, tzinfo=UTC),
            create_if_not_exists=True,
        )
        assert _body(api.requests[0]) == {
            "expires_at": "2026-10-14T12:00:00Z",
            "create_if_not_exists": True,
        }
        assert token == SandboxPreviewTokenInfo(
            name="t1",
            token="secret",
            expires_at=datetime(2026, 10, 14, 12, tzinfo=UTC),
            expired=False,
        )
        assert [t.name for t in previews.list_tokens(preview_name="web")] == ["t1"]
        previews.delete_token(preview_name="web", token_name="t1")
        assert api.requests[2].method == "DELETE"

    def test_fails_every_call_on_sandbox_from_url(self) -> None:
        api = _Api()
        previews = api.sandbox().previews
        calls: dict[str, Callable[[], object]] = {
            "create": lambda: previews.create(port=3000),
            "get_info": lambda: previews.get_info(name="web"),
            "list": lambda: next(previews.list()),
            "update": lambda: previews.update(name="web"),
            "delete": lambda: previews.delete(name="web"),
            "create_token": lambda: previews.create_token(
                preview_name="web", expires_at=datetime.now(UTC)
            ),
            "list_tokens": lambda: next(previews.list_tokens(preview_name="web")),
            "delete_token": lambda: previews.delete_token(
                preview_name="web", token_name="t1"
            ),
        }
        for name, call in calls.items():
            with pytest.raises(ValueError, match="sandbox_from_url"):
                call()
            assert api.requests == [], name


class TestAsyncSandboxPreviews:
    @pytest.mark.asyncio
    async def test_creates_lists_and_deletes_tokens(self) -> None:
        api = _Api()
        api.route("POST", _PREVIEWS, _PREVIEW, status=201)
        api.route(
            "GET",
            f"{_PREVIEWS}/web/tokens",
            {"items": [_TOKEN], "pagination": {"has_more": False}},
        )
        api.routes[("DELETE", f"{_PREVIEWS}/web/tokens/t1")] = lambda request: (
            httpx.Response(204)
        )
        previews = (await api.async_named_sandbox()).previews
        info = await previews.create(port=3000, public=True)
        assert info.name == "web"
        assert _body(api.requests[0]) == {"port": 3000, "public": True}
        names = [t.name async for t in previews.list_tokens(preview_name="web")]
        assert names == ["t1"]
        await previews.delete_token(preview_name="web", token_name="t1")
        assert api.requests[2].url.path == f"{_PREVIEWS}/web/tokens/t1"

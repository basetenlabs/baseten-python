from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from baseten.sandbox import (
    AsyncSandboxClient,
    SandboxAPIError,
    SandboxClient,
    VolumeInfo,
)

_VOLUMES = "/v1/sandboxes/volumes"

_VOLUME = {
    "name": "data",
    "size": 1024,
    "region": "us-pdx-1",
    "status": "DEPLOYED",
    "attached_to": "sb",
    "labels": {"k": "v"},
    "created_at": "2026-10-01T00:00:00Z",
    "created_by": "me",
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

    def client(self, **options: Any) -> SandboxClient:
        return SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
            **options,
        )

    def async_client(self) -> AsyncSandboxClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            return self.handle(request)

        return AsyncSandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


class TestVolumeClient:
    def test_creates_sending_only_set_fields_and_maps_record(self) -> None:
        api = _Api()
        api.route("POST", _VOLUMES, _VOLUME, status=201)
        info = api.client(team_id="team-1").volumes.create(
            name="data", size=1024, labels={}
        )
        request = api.requests[0]
        assert dict(request.url.params) == {"team_id": "team-1"}
        assert _body(request) == {"name": "data", "size": 1024, "labels": {}}
        assert info == VolumeInfo(
            name="data",
            size=1024,
            region="us-pdx-1",
            status="DEPLOYED",
            attached_to="sb",
            labels={"k": "v"},
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
            updated_at=None,
            created_by="me",
            updated_by=None,
        )

    def test_creates_with_a_string_size(self) -> None:
        api = _Api()
        api.route(
            "POST",
            _VOLUMES,
            {"name": "data", "size": "10Gi", "region": "r", "status": "DEPLOYING"},
            status=201,
        )
        info = api.client().volumes.create(size="10Gi", region="r")
        assert _body(api.requests[0]) == {"size": "10Gi", "region": "r"}
        assert info.size == "10Gi"

    def test_gets_info(self) -> None:
        api = _Api()
        api.route("GET", f"{_VOLUMES}/data", _VOLUME)
        assert api.client().volumes.get_info(name="data").name == "data"
        assert "team_id" not in api.requests[0].url.params

    def test_lists_across_pages_with_filters_omitting_labels(self) -> None:
        api = _Api()
        pages = iter(
            [
                {
                    "items": [
                        {"name": "a", "size": 1, "region": "r", "status": "DEPLOYED"}
                    ],
                    "pagination": {"has_more": True, "cursor": "c1"},
                },
                {
                    "items": [
                        {"name": "b", "size": 1, "region": "r", "status": "FAILED"}
                    ],
                    "pagination": {"has_more": False},
                },
            ]
        )
        api.routes[("GET", _VOLUMES)] = lambda request: httpx.Response(
            200, json=next(pages)
        )
        volumes = list(
            api.client().volumes.list(
                query="x", statuses=["DEPLOYED", "FAILED"], page_size=1
            )
        )
        assert [volume.name for volume in volumes] == ["a", "b"]
        # List omits labels, distinct from a volume with none.
        assert [volume.labels for volume in volumes] == [None, None]
        first, second = api.requests
        assert first.url.params.get_list("status") == ["DEPLOYED", "FAILED"]
        assert (first.url.params["q"], first.url.params["limit"]) == ("x", "1")
        assert "cursor" not in first.url.params
        assert second.url.params["cursor"] == "c1"

    def test_updates_sending_only_set_fields(self) -> None:
        api = _Api()
        api.route("PATCH", f"{_VOLUMES}/data", _VOLUME)
        volumes = api.client().volumes
        volumes.update(name="data", size="20Gi", labels={})
        volumes.update(name="data", labels={"k": "v"})
        volumes.update(name="data")
        assert _body(api.requests[0]) == {"size": "20Gi", "labels": {}}
        assert _body(api.requests[1]) == {"labels": {"k": "v"}}
        assert _body(api.requests[2]) == {}

    def test_deletes_returning_deleting_record(self) -> None:
        api = _Api()
        api.route(
            "DELETE",
            f"{_VOLUMES}/data",
            {"name": "data", "size": 1024, "region": "r", "status": "DELETING"},
            status=202,
        )
        assert api.client().volumes.delete(name="data").status == "DELETING"

    def test_raises_api_errors(self) -> None:
        api = _Api()
        api.route(
            "DELETE",
            f"{_VOLUMES}/data",
            {"code": "RESOURCE_IN_USE", "message": "mounted"},
            status=409,
        )
        with pytest.raises(SandboxAPIError) as raised:
            api.client().volumes.delete(name="data")
        assert raised.value.status == 409


class TestAsyncVolumeClient:
    @pytest.mark.asyncio
    async def test_creates_gets_lists_updates_and_deletes(self) -> None:
        api = _Api()
        api.route("POST", _VOLUMES, _VOLUME, status=201)
        api.route("GET", f"{_VOLUMES}/data", _VOLUME)
        api.route(
            "GET", _VOLUMES, {"items": [_VOLUME], "pagination": {"has_more": False}}
        )
        api.route("PATCH", f"{_VOLUMES}/data", _VOLUME)
        api.route(
            "DELETE", f"{_VOLUMES}/data", {**_VOLUME, "status": "DELETING"}, status=202
        )
        volumes = api.async_client().volumes
        assert (await volumes.create(size="1Gi")).name == "data"
        assert (await volumes.get_info(name="data")).name == "data"
        assert [volume.name async for volume in volumes.list()] == ["data"]
        assert (await volumes.update(name="data", size=2048)).name == "data"
        assert (await volumes.delete(name="data")).status == "DELETING"
        assert _body(api.requests[0]) == {"size": "1Gi"}
        assert _body(api.requests[3]) == {"size": 2048}

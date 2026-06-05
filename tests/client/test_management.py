from __future__ import annotations

import json

import pytest

import baseten.client.managementapi
from baseten.client import AsyncManagementClient, ManagementClient
from tests.conftest import FakeTransport

MINIMAL_MODEL = {
    "id": "model-1",
    "name": "my-model",
    "created_at": "2024-01-01T00:00:00Z",
    "deployments_count": 2,
    "production_deployment_id": "dep-1",
    "development_deployment_id": "dep-2",
    "instance_type_name": "A10G",
    "team_name": "my-team",
}

MINIMAL_SECRET = {
    "name": "MY_SECRET",
    "created_at": "2024-01-01T00:00:00Z",
    "team_name": "my-team",
}


def make_sync_client(fake: FakeTransport) -> ManagementClient:
    client = ManagementClient(api_key="test-key")
    client.http_client._transport = fake.sync_transport  # type: ignore[attr-defined]
    return client


def make_async_client(fake: FakeTransport) -> AsyncManagementClient:
    client = AsyncManagementClient(api_key="test-key")
    client.http_client._transport = fake.async_transport  # type: ignore[attr-defined]
    return client


def test_get_models_sync() -> None:
    fake = FakeTransport(200, {"models": [MINIMAL_MODEL]})
    client = make_sync_client(fake)

    resp = client.api.get_models()
    assert resp.models is not None
    assert len(resp.models) == 1
    assert resp.models[0].name == "my-model"
    assert fake.capture.method == "GET"
    assert fake.capture.path == "/v1/models"
    assert fake.capture.headers["authorization"] == "Bearer test-key"
    client.close()


def test_response_error_sync() -> None:
    fake = FakeTransport(500, {"detail": "boom"})
    client = make_sync_client(fake)

    with pytest.raises(baseten.client.managementapi.ResponseError) as exc_info:
        client.api.get_models()
    assert exc_info.value.status_code == 500
    assert "boom" in exc_info.value.body
    client.close()


@pytest.mark.asyncio
async def test_get_models() -> None:
    fake = FakeTransport(200, {"models": [MINIMAL_MODEL]})
    client = make_async_client(fake)

    resp = await client.api.get_models()
    assert resp.models is not None
    assert len(resp.models) == 1
    assert resp.models[0].name == "my-model"
    assert fake.capture.method == "GET"
    assert fake.capture.path == "/v1/models"
    assert fake.capture.headers["authorization"] == "Bearer test-key"
    await client.close()


@pytest.mark.asyncio
async def test_path_params_escaped() -> None:
    fake = FakeTransport(200, MINIMAL_MODEL)
    client = make_async_client(fake)

    await client.api.get_models_model_id(model_id="abc/def")
    assert fake.capture.path == "/v1/models/abc%2Fdef"
    await client.close()


@pytest.mark.asyncio
async def test_post_with_body() -> None:
    fake = FakeTransport(200, MINIMAL_SECRET)
    client = make_async_client(fake)

    resp = await client.api.post_secrets(
        body=baseten.client.managementapi.UpsertSecretRequest(
            name="MY_SECRET",
            value="s3cret",
        ),
    )

    assert resp.name == "MY_SECRET"
    assert fake.capture.method == "POST"
    assert fake.capture.headers["content-type"] == "application/json"
    body = json.loads(fake.capture.body)
    assert body["name"] == "MY_SECRET"
    assert body["value"] == "s3cret"
    await client.close()


@pytest.mark.asyncio
async def test_response_error() -> None:
    fake = FakeTransport(500, {"detail": "boom"})
    client = make_async_client(fake)

    with pytest.raises(baseten.client.managementapi.ResponseError) as exc_info:
        await client.api.get_models()
    assert exc_info.value.status_code == 500
    assert "boom" in exc_info.value.body
    await client.close()


@pytest.mark.asyncio
async def test_unexpected_content_type() -> None:
    fake = FakeTransport(200, None)
    client = make_async_client(fake)

    with pytest.raises(ValueError, match="content type"):
        await client.api.get_models()
    await client.close()

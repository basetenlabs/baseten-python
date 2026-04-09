from __future__ import annotations

import json

import pytest

import baseten.client.inferenceapi
from baseten.client import AsyncInferenceClient, InferenceClient
from baseten.client.inferenceapi import PredictInput
from tests.conftest import FakeTransport


def make_sync_client(fake: FakeTransport) -> InferenceClient:
    client = InferenceClient(api_key="test-key", model_id="abc123")
    client.http_client._transport = fake.sync_transport  # type: ignore[attr-defined]
    return client


def make_async_client(fake: FakeTransport) -> AsyncInferenceClient:
    client = AsyncInferenceClient(api_key="test-key", model_id="abc123")
    client.http_client._transport = fake.async_transport  # type: ignore[attr-defined]
    return client


def test_predict_production_sync() -> None:
    fake = FakeTransport(200, {"result": 42})
    client = make_sync_client(fake)

    resp = client.api.predict_production(body=PredictInput({"input": "hello"}))
    assert resp.root["result"] == 42
    assert fake.capture.method == "POST"
    assert fake.capture.path == "/production/predict"
    assert fake.capture.headers["authorization"] == "Api-Key test-key"
    body = json.loads(fake.capture.body)
    assert body == {"input": "hello"}
    client.close()


@pytest.mark.asyncio
async def test_predict_production() -> None:
    fake = FakeTransport(200, {"result": 42})
    client = make_async_client(fake)

    resp = await client.api.predict_production(body=PredictInput({"input": "hello"}))
    assert resp.root["result"] == 42
    assert fake.capture.method == "POST"
    assert fake.capture.path == "/production/predict"
    body = json.loads(fake.capture.body)
    assert body == {"input": "hello"}
    await client.close()


@pytest.mark.asyncio
async def test_async_predict_201() -> None:
    fake = FakeTransport(201, {"request_id": "req-123"})
    client = make_async_client(fake)

    resp = await client.api.async_predict_production(
        body=baseten.client.inferenceapi.AsyncPredictRequest(
            model_input={"prompt": "test"},
        ),
    )
    assert resp.request_id == "req-123"
    body = json.loads(fake.capture.body)
    assert body["model_input"] == {"prompt": "test"}
    await client.close()


@pytest.mark.asyncio
async def test_typed_error() -> None:
    fake = FakeTransport(429, {"error": "rate limited", "error_code": "rate_limited"})
    client = make_async_client(fake)

    with pytest.raises(baseten.client.inferenceapi.ResponseErrorResponse) as exc_info:
        await client.api.predict_production(body=PredictInput({}))
    assert exc_info.value.status_code == 429
    assert exc_info.value.error_response.error == "rate limited"
    await client.close()


@pytest.mark.asyncio
async def test_unknown_status_falls_back_to_response_error() -> None:
    fake = FakeTransport(418, {"error": "teapot"})
    client = make_async_client(fake)

    with pytest.raises(baseten.client.inferenceapi.ResponseError) as exc_info:
        await client.api.predict_production(body=PredictInput({}))
    assert exc_info.value.status_code == 418
    assert "teapot" in exc_info.value.body
    await client.close()


@pytest.mark.asyncio
async def test_wake_no_response() -> None:
    fake = FakeTransport(202)
    client = make_async_client(fake)

    await client.api.wake_production()
    assert fake.capture.method == "POST"
    assert fake.capture.path == "/production/wake"
    assert fake.capture.body == ""
    await client.close()


@pytest.mark.asyncio
async def test_wake_error() -> None:
    fake = FakeTransport(401, {"error": "unauthorized", "error_code": "unauthorized"})
    client = make_async_client(fake)

    with pytest.raises(baseten.client.inferenceapi.ResponseErrorResponse) as exc_info:
        await client.api.wake_production()
    assert exc_info.value.status_code == 401
    await client.close()

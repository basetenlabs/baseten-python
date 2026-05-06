from __future__ import annotations

import dataclasses

import pytest

from baseten.client import (
    AsyncInferenceClient,
    AsyncManagementClient,
    InferenceClient,
    InferenceClientOptions,
    ManagementClient,
    ManagementClientOptions,
)


def test_management_default_base_url() -> None:
    client = ManagementClient(api_key="test-key")
    assert client.options.base_url == ManagementClient.default_base_url()
    client.close()


def test_management_base_url_override() -> None:
    client = ManagementClient(
        api_key="test-key", base_url_override="https://custom.example.com"
    )
    assert client.options.base_url == "https://custom.example.com"
    assert client.options.base_url_override == "https://custom.example.com"
    client.close()


def test_management_options_frozen() -> None:
    client = ManagementClient(api_key="test-key")
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.options.api_key = "other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    client.close()


def test_management_close_http_client_default_true() -> None:
    client = ManagementClient(api_key="test-key")
    assert client.close_http_client_on_close is True
    client.close()


def test_management_close_http_client_default_false_when_provided() -> None:
    import httpx

    http_client = httpx.Client()
    client = ManagementClient(api_key="test-key", http_client_override=http_client)
    assert client.close_http_client_on_close is False
    client.close()
    http_client.close()


def test_management_context_manager() -> None:
    with ManagementClient(api_key="test-key") as client:
        assert client.api is not None


def test_management_options_splat() -> None:
    opts = ManagementClientOptions(
        api_key="test-key",
        headers={"X-Custom": "v"},
        base_url_override="https://custom.example.com",
    )
    client = ManagementClient(**dataclasses.asdict(opts))
    assert client.options.api_key == "test-key"
    assert client.options.headers == {"X-Custom": "v"}
    assert client.options.base_url == "https://custom.example.com"
    assert client.http_client.headers["X-Custom"] == "v"
    client.close()


def test_management_user_agent_default() -> None:
    client = ManagementClient(api_key="test-key")
    assert client.http_client.headers["User-Agent"].startswith("baseten-python/")
    client.close()


def test_management_user_agent_user_override() -> None:
    client = ManagementClient(api_key="test-key", headers={"User-Agent": "custom/1.0"})
    assert client.http_client.headers["User-Agent"] == "custom/1.0"
    client.close()


def test_management_empty_api_key_skips_authorization() -> None:
    client = ManagementClient(api_key="")
    assert "authorization" not in client.http_client.headers
    client.close()


@pytest.mark.asyncio
async def test_async_management_default_base_url() -> None:
    client = AsyncManagementClient(api_key="test-key")
    assert client.options.base_url == AsyncManagementClient.default_base_url()
    await client.close()


@pytest.mark.asyncio
async def test_async_management_context_manager() -> None:
    async with AsyncManagementClient(api_key="test-key") as client:
        assert client.api is not None


def test_inference_default_base_url_model() -> None:
    client = InferenceClient(api_key="test-key", model_id="abc123")
    assert client.options.base_url == "https://model-abc123.api.baseten.co"
    client.close()


def test_inference_default_base_url_model_with_env() -> None:
    client = InferenceClient(api_key="test-key", model_id="abc", environment="prod-us")
    assert client.options.base_url == "https://model-abc-prod-us.api.baseten.co"
    client.close()


def test_inference_default_base_url_chain() -> None:
    client = InferenceClient(api_key="test-key", chain_id="def456")
    assert client.options.base_url == "https://chain-def456.api.baseten.co"
    client.close()


def test_inference_base_url_override() -> None:
    client = InferenceClient(
        api_key="test-key", base_url_override="https://custom.example.com"
    )
    assert client.options.base_url == "https://custom.example.com"
    client.close()


def test_inference_base_url_override_wins_over_ids() -> None:
    client = InferenceClient(
        api_key="test-key",
        model_id="abc",
        base_url_override="https://custom.example.com",
    )
    assert client.options.base_url == "https://custom.example.com"
    client.close()


def test_inference_requires_model_or_chain_without_override() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        InferenceClient(api_key="test-key")


def test_inference_model_and_chain_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        InferenceClient(api_key="test-key", model_id="abc", chain_id="def")


def test_inference_options_splat() -> None:
    opts = InferenceClientOptions(
        api_key="test-key",
        headers={"X-Custom": "v"},
        model_id="abc",
        environment="prod-us",
    )
    client = InferenceClient(**dataclasses.asdict(opts))
    assert client.options.headers == {"X-Custom": "v"}
    assert client.options.base_url == "https://model-abc-prod-us.api.baseten.co"
    assert client.http_client.headers["X-Custom"] == "v"
    client.close()


def test_inference_user_agent_default() -> None:
    client = InferenceClient(api_key="test-key", model_id="abc")
    assert client.http_client.headers["User-Agent"].startswith("baseten-python/")
    client.close()


def test_inference_empty_api_key_skips_authorization() -> None:
    client = InferenceClient(api_key="", model_id="abc")
    assert "authorization" not in client.http_client.headers
    client.close()


def test_inference_options_frozen() -> None:
    client = InferenceClient(api_key="test-key", model_id="abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.options.api_key = "other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    client.close()


def test_inference_context_manager() -> None:
    with InferenceClient(api_key="test-key", model_id="abc") as client:
        assert client.api is not None


@pytest.mark.asyncio
async def test_async_inference_context_manager() -> None:
    async with AsyncInferenceClient(api_key="test-key", model_id="abc") as client:
        assert client.api is not None


@pytest.mark.asyncio
async def test_async_inference_default_base_url() -> None:
    client = AsyncInferenceClient(
        api_key="test-key", chain_id="xyz", environment="staging"
    )
    assert client.options.base_url == "https://chain-xyz-staging.api.baseten.co"
    await client.close()

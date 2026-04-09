"""End-to-end tests against a live Baseten environment.

Requires the following environment variables:
  BASETEN_E2E_TEST_API_KEY  — API key for authentication
  BASETEN_E2E_TEST_DOMAIN   — Domain (e.g. baseten.co)
  BASETEN_E2E_TEST_MODEL_ID — Model ID of a deployed model (bootstrap first)

Skipped when BASETEN_E2E_TEST_API_KEY is empty or unset. If the API key is
set but either DOMAIN or MODEL_ID is missing, the test errors.
"""

from __future__ import annotations

import functools
import inspect
import os
import uuid
from typing import Any, Callable

import pytest

import baseten.client.inferenceapi
import baseten.client.managementapi
from baseten.client import (
    AsyncInferenceClient,
    AsyncManagementClient,
    ManagementClient,
)

API_KEY = os.environ.get("BASETEN_E2E_TEST_API_KEY", "")
DOMAIN = os.environ.get("BASETEN_E2E_TEST_DOMAIN", "")
MODEL_ID = os.environ.get("BASETEN_E2E_TEST_MODEL_ID", "")


def ensure_e2e_env() -> None:
    if not API_KEY:
        pytest.skip("BASETEN_E2E_TEST_API_KEY not set")
    if not DOMAIN:
        raise EnvironmentError(
            "BASETEN_E2E_TEST_API_KEY is set but BASETEN_E2E_TEST_DOMAIN is missing"
        )
    if not MODEL_ID:
        raise EnvironmentError(
            "BASETEN_E2E_TEST_API_KEY is set but BASETEN_E2E_TEST_MODEL_ID is missing"
        )


def e2e(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            ensure_e2e_env()
            return await fn(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ensure_e2e_env()
        return fn(*args, **kwargs)

    return wrapper


def management_client() -> ManagementClient:
    return ManagementClient(
        api_key=API_KEY,
        base_url_override=f"https://api.{DOMAIN}",
    )


def async_management_client() -> AsyncManagementClient:
    return AsyncManagementClient(
        api_key=API_KEY,
        base_url_override=f"https://api.{DOMAIN}",
    )


def async_inference_client() -> AsyncInferenceClient:
    return AsyncInferenceClient(
        api_key=API_KEY,
        base_url_override=f"https://model-{MODEL_ID}.api.{DOMAIN}",
    )


@e2e
def test_list_models() -> None:
    with management_client() as client:
        models = client.api.get_models()
        assert models.models is not None
        ids = [m.id for m in models.models]
        assert MODEL_ID in ids


@e2e
def test_get_model() -> None:
    with management_client() as client:
        model = client.api.get_models_model_id(model_id=MODEL_ID)
        assert model.id == MODEL_ID
        assert model.name is not None


@e2e
def test_get_model_not_found() -> None:
    with management_client() as client:
        with pytest.raises(baseten.client.managementapi.ResponseError) as exc_info:
            client.api.get_models_model_id(model_id="nonexistent-model-id")
        assert exc_info.value.status_code == 404


@e2e
def test_list_deployments() -> None:
    with management_client() as client:
        deployments = client.api.get_models_deployments(model_id=MODEL_ID)
        assert deployments.deployments is not None
        assert len(deployments.deployments) > 0


@e2e
@pytest.mark.asyncio
async def test_get_model_async() -> None:
    async with async_management_client() as client:
        model = await client.api.get_models_model_id(model_id=MODEL_ID)
        assert model.id == MODEL_ID
        assert model.name is not None


@e2e
@pytest.mark.asyncio
async def test_inference() -> None:
    async with async_inference_client() as client:
        result = await client.api.predict_production(
            body=baseten.client.inferenceapi.PredictInput({"prompt": "hello"}),
        )
        assert result.root is not None


@e2e
@pytest.mark.asyncio
async def test_api_key_crud() -> None:
    key_name = f"e2e-test-{uuid.uuid4()}"
    created_prefix: str | None = None

    async with async_management_client() as client:
        try:
            created = await client.api.post_api_keys(
                body=baseten.client.managementapi.CreateAPIKeyRequest(
                    name=key_name,
                    type=baseten.client.managementapi.APIKeyCategory.PERSONAL,
                ),
            )
            assert created.api_key
            created_prefix = created.api_key.split(".")[0]

            keys = await client.api.get_api_keys()
            names = [k.name for k in keys.keys]
            assert key_name in names

            tombstone = await client.api.delete_api_keys(
                api_key_prefix=created_prefix,
            )
            assert tombstone.prefix == created_prefix

            keys = await client.api.get_api_keys()
            prefixes = [k.prefix for k in keys.keys]
            assert created_prefix not in prefixes
            created_prefix = None
        finally:
            if created_prefix is not None:
                try:
                    await client.api.delete_api_keys(
                        api_key_prefix=created_prefix,
                    )
                except Exception:
                    pass

from __future__ import annotations

import json

import httpx
import pytest

import baseten.client.managementapi
import baseten.client.sandboxapi
from baseten.sandbox import SandboxAPIError, SandboxError, SandboxGatewayError
from baseten.sandbox._errors import api_error_from, converted_errors


class TestControlPlaneErrors:
    def test_carries_status_code_message_and_details(self) -> None:
        body = {"code": "NOT_FOUND", "message": "no such sandbox", "details": {"a": 1}}
        err = _control_error(404, json.dumps(body))
        assert type(err) is SandboxAPIError
        assert err.status == 404
        assert err.code == "NOT_FOUND"
        assert err.message == "no such sandbox"
        assert err.details == {"a": 1}
        assert err.body == body
        assert str(err) == "sandbox API error (HTTP 404) NOT_FOUND: no such sandbox"
        assert isinstance(err, SandboxError)

    def test_takes_code_from_error_field_alongside_message(self) -> None:
        err = _control_error(
            409, json.dumps({"error": "CONFLICT", "message": "already exists"})
        )
        assert err.code == "CONFLICT"
        assert err.message == "already exists"

    def test_keeps_body_that_is_not_json_as_text(self) -> None:
        err = _control_error(500, "upstream failed")
        assert err.body == "upstream failed"
        assert err.code is None
        assert err.message is None
        assert str(err) == "sandbox API error (HTTP 500)"

    def test_gateway_status_is_not_gateway_error(self) -> None:
        assert type(_control_error(503, "")) is SandboxAPIError


class TestSandboxErrors:
    def test_takes_message_from_error_field(self) -> None:
        err = api_error_from(
            baseten.client.sandboxapi.ResponseErrorResponse(
                status_code=404,
                error_response=baseten.client.sandboxapi.ErrorResponse(
                    error="process not found"
                ),
            ),
            "exec",
        )
        assert err.code is None
        assert err.message == "process not found"
        assert err.body == {"error": "process not found"}
        assert str(err) == "sandbox API error (HTTP 404): process not found"

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_converts_edge_gateway_status_to_gateway_error(self, status: int) -> None:
        err = api_error_from(
            baseten.client.sandboxapi.ResponseError(status_code=status, body="<html>"),
            "exec",
        )
        assert type(err) is SandboxGatewayError
        assert err.status == status
        assert err.body == "<html>"


def test_converted_errors_chains_generated_error() -> None:
    generated = baseten.client.managementapi.ResponseError(status_code=400, body="{}")
    with pytest.raises(SandboxAPIError) as info, converted_errors("control"):
        raise generated
    assert info.value.__cause__ is generated


def test_converted_errors_passes_network_failure_through() -> None:
    failure = httpx.ConnectError("refused")
    with pytest.raises(httpx.ConnectError) as info, converted_errors("exec"):
        raise failure
    assert info.value is failure


def _control_error(status: int, body: str) -> SandboxAPIError:
    return api_error_from(
        baseten.client.managementapi.ResponseError(status_code=status, body=body),
        "control",
    )

from __future__ import annotations

import json

from pydantic import BaseModel

import baseten.client.managementapi
import baseten.client.sandboxapi


class SandboxApiError(Exception):
    """An error response from the sandbox control plane or from a sandbox.

    The response body is parsed as JSON when possible, otherwise kept as
    text. *code* holds the machine-readable error code when the response has
    one; the control plane puts it in ``code`` or ``error``, a sandbox in
    ``error``.
    """

    def __init__(self, status: int, body: object) -> None:
        fields = body if isinstance(body, dict) else {}
        body_message = fields.get("message")
        body_error = fields.get("error")
        body_code = fields.get("code")
        code = (
            body_code
            if isinstance(body_code, str)
            else (body_error if isinstance(body_message, str) else None)
        )
        description = None
        if isinstance(body_message, str):
            description = body_message
        elif isinstance(body_error, str):
            description = body_error
        message = f"sandbox API error (HTTP {status})"
        if isinstance(code, str):
            message += f" {code}"
        if description is not None:
            message += f": {description}"
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = fields.get("details")
        self.body = body


class SandboxGatewayError(SandboxApiError):
    """A 502, 503, or 504 from the edge in front of a sandbox.

    Not from the sandbox itself: usually transient, for example while a
    sandbox wakes from standby or when a request outlasts the edge's timeout.
    """


_GATEWAY_STATUSES = frozenset({502, 503, 504})


def to_sandbox_api_error(error: object, plane: str) -> Exception:
    """Convert an error raised by a generated client, or return it unchanged.

    ``plane`` is ``"control"`` or ``"exec"``. Only a sandbox sits behind the
    edge, so gateway statuses from the control plane stay ordinary errors.
    """
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        return error if isinstance(error, Exception) else Exception(str(error))
    if isinstance(
        error,
        (
            baseten.client.managementapi.ResponseError,
            baseten.client.sandboxapi.ResponseError,
        ),
    ):
        try:
            body: object = json.loads(error.body)
        except (json.JSONDecodeError, TypeError):
            body = error.body
    else:
        # Generated typed errors are dataclasses of status_code plus the
        # error's parsed model, keyed by the schema name.
        model_fields = {
            name: value for name, value in vars(error).items() if name != "status_code"
        }
        if len(model_fields) == 1:
            body = next(iter(model_fields.values()))
        else:  # pragma: no cover - generated errors carry exactly one model
            body = model_fields
    if isinstance(body, BaseModel):
        # The curated error reads code and details off a plain dict.
        body = body.model_dump()
    if plane == "exec" and status in _GATEWAY_STATUSES:
        return SandboxGatewayError(status, body)
    return SandboxApiError(status, body)

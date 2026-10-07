from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal, Self

import baseten.client.managementapi
import baseten.client.sandboxapi

GENERATED_RESPONSE_ERRORS = (
    baseten.client.managementapi.ResponseError,
    baseten.client.sandboxapi.ResponseError,
    baseten.client.sandboxapi.ResponseErrorResponse,
)
"""Errors the generated clients raise for an error response."""

_GATEWAY_STATUSES = frozenset({502, 503, 504})


class SandboxError(Exception):
    """Base class for every error the sandbox SDK raises itself."""


class SandboxAPIError(SandboxError):
    """An error response from the sandbox control plane or from a sandbox."""

    status: int
    """HTTP status of the response."""

    code: str | None
    """Machine-readable error code, when the response has one."""

    message: str | None
    """Description of the error, when the response has one."""

    details: Any
    """Additional error details, when the response has them."""

    body: Any
    """Response body, parsed as JSON when possible, otherwise the raw text."""

    def __init__(
        self,
        *,
        status: int,
        code: str | None,
        message: str | None,
        details: Any,
        body: Any,
    ) -> None:
        """Create an error.

        Args:
            status: HTTP status of the response.
            code: Machine-readable error code, when the response has one.
            message: Description of the error, when the response has one.
            details: Additional error details, when the response has them.
            body: Response body, parsed as JSON when possible, otherwise the
                raw text.
        """
        text = f"sandbox API error (HTTP {status})"
        if code is not None:
            text += f" {code}"
        if message is not None:
            text += f": {message}"
        super().__init__(text)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.body = body

    @classmethod
    def _from_response(cls, *, status: int, body: Any) -> Self:
        """Create an error from a response, reading its fields from the body."""
        fields: dict[str, Any] = body if isinstance(body, dict) else {}
        body_message = fields.get("message")
        if not isinstance(body_message, str):
            body_message = None
        body_error = fields.get("error")
        if not isinstance(body_error, str):
            body_error = None
        # The control plane puts the code in error and the description in
        # message. A sandbox puts the description in error.
        code = fields.get("code")
        if not isinstance(code, str):
            code = body_error if body_message is not None else None
        return cls(
            status=status,
            code=code,
            message=body_message if body_message is not None else body_error,
            details=fields.get("details"),
            body=body,
        )


class SandboxGatewayError(SandboxAPIError):
    """A 502, 503, or 504 from the edge in front of a sandbox.

    Comes from the edge rather than from the sandbox itself, and is usually
    transient, for example while a sandbox wakes from standby or when a
    request outlasts the edge's timeout.
    """


class SandboxProcessWaitTimeoutError(SandboxError):
    """A process still running when a wait for it ran out of time.

    The process keeps running regardless. The last error the wait rode out,
    if any, is chained as the cause.
    """

    identifier: str
    """PID or name of the process waited on."""

    def __init__(self, *, identifier: str) -> None:
        """Create an error.

        Args:
            identifier: PID or name of the process waited on.
        """
        super().__init__(
            f"process {identifier} had not finished when the wait timed out; "
            "it may still be running"
        )
        self.identifier = identifier


class ImageUploadError(SandboxError):
    """A failed upload of an image's build context.

    The upload goes to storage rather than to the API, so its error body has
    no fixed shape. The image stays as it was left, so push it again or
    delete it.
    """

    image_name: str
    """Name of the image whose build context was being uploaded."""

    status: int
    """HTTP status of the storage response."""

    body: str
    """Raw text of the storage response body."""

    def __init__(self, *, image_name: str, status: int, body: str) -> None:
        """Create an error.

        Args:
            image_name: Name of the image whose build context was being
                uploaded.
            status: HTTP status of the storage response.
            body: Raw text of the storage response body.
        """
        super().__init__(
            f"uploading the source of image {image_name} failed (HTTP {status}); "
            "the image stays as it was left, so push it again or delete it"
        )
        self.image_name = image_name
        self.status = status
        self.body = body


class ImageBuildError(SandboxError):
    """An image that did not become ready to use.

    Either its build failed, or it was still processing when the wait ran out
    of time. :meth:`ImageClient.logs` shows the build's output.
    """

    image_name: str
    """Name of the image."""

    status: str
    """Status last seen, ``FAILED`` when the build failed."""

    timed_out: bool
    """Whether the wait ran out of time. Processing continues regardless."""

    def __init__(self, *, image_name: str, status: str, timed_out: bool) -> None:
        """Create an error.

        Args:
            image_name: Name of the image.
            status: Status last seen, ``FAILED`` when the build failed.
            timed_out: Whether the wait ran out of time.
        """
        super().__init__(
            f"image {image_name} was still {status} when the wait timed out; "
            "it may still finish"
            if timed_out
            else f"image {image_name} failed to build (status {status})"
        )
        self.image_name = image_name
        self.status = status
        self.timed_out = timed_out


@contextmanager
def converted_errors(plane: Literal["control", "exec"]) -> Iterator[None]:
    """Raise generated clients' error responses as :class:`SandboxAPIError`.

    Anything else, such as a network failure, passes through unchanged.
    """
    try:
        yield
    except GENERATED_RESPONSE_ERRORS as err:
        raise api_error_from(err, plane) from err


def api_error_from(
    err: baseten.client.managementapi.ResponseError
    | baseten.client.sandboxapi.ResponseError
    | baseten.client.sandboxapi.ResponseErrorResponse,
    plane: Literal["control", "exec"],
) -> SandboxAPIError:
    """Convert an error a generated client raised for an error response."""
    body: Any
    if isinstance(err, baseten.client.sandboxapi.ResponseErrorResponse):
        body = err.error_response.model_dump(mode="json")
    else:
        try:
            body = json.loads(err.body)
        except ValueError:
            body = err.body
    # Only a sandbox sits behind the edge, so the same statuses from the
    # control plane are ordinary errors.
    if plane == "exec" and err.status_code in _GATEWAY_STATUSES:
        return SandboxGatewayError._from_response(status=err.status_code, body=body)
    return SandboxAPIError._from_response(status=err.status_code, body=body)

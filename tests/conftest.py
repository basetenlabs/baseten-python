from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class CapturedRequest:
    method: str = ""
    path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


class FakeTransport:
    """Mock HTTP transport that captures requests and returns fixed responses."""

    def __init__(self, status_code: int = 200, response: Any = None) -> None:
        self.status_code = status_code
        self.response = response
        self.capture = CapturedRequest()

    def _build_response(self, request: httpx.Request) -> httpx.Response:
        self.capture = CapturedRequest(
            method=request.method,
            path=str(request.url.raw_path, "ascii"),
            headers={k: v for k, v in request.headers.items()},
            body=request.content.decode() if request.content else "",
        )
        body = b""
        headers: dict[str, str] = {}
        if self.response is not None:
            body = json.dumps(self.response).encode()
            headers["content-type"] = "application/json"
        return httpx.Response(
            status_code=self.status_code,
            content=body,
            headers=headers,
        )

    @property
    def sync_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._build_response)

    @property
    def async_transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            return self._build_response(request)

        return httpx.MockTransport(handler)

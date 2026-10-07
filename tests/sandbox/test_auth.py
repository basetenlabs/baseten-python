from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import baseten.client.managementapi
import baseten.sandbox._auth
from baseten.sandbox import SandboxTokenProviderContext
from baseten.sandbox._auth import (
    AsyncTokenAuth,
    AsyncTokenSource,
    TokenAuth,
    TokenSource,
)


def _revoked(request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(401, headers={"x-blaxel-error-code": "TOKEN_REVOKED"})


@pytest.fixture(autouse=True)
def no_revoked_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baseten.sandbox._auth, "_token_revoked_retry_delay", 0.0)


class _Server:
    """Mints numbered tokens at ``/v1/token`` and answers other requests."""

    def __init__(
        self,
        *,
        expires_in: timedelta = timedelta(hours=2),
        respond: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.expires_in = expires_in
        self.respond = respond or (lambda request: httpx.Response(200))
        self.mints: list[httpx.Request] = []
        self.requests: list[httpx.Request] = []
        self.authorizations: list[str | None] = []
        self.mint_failures = 0
        self.mint_gate: threading.Event | None = None
        """When set, each mint waits for it."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            if self.mint_gate is not None:
                self.mint_gate.wait()
            self.mints.append(request)
            if self.mint_failures > 0:
                self.mint_failures -= 1
                return httpx.Response(500, text="mint failed")
            return httpx.Response(
                200,
                json={
                    "token": f"token-{len(self.mints)}",
                    "expires_at": (datetime.now(UTC) + self.expires_in).isoformat(),
                },
            )
        self.requests.append(request)
        # A resend reuses the request object, so its header is read now.
        self.authorizations.append(request.headers.get("Authorization"))
        return self.respond(request)

    def tokens(self, *, api_key: str = "key", provider=None) -> TokenSource:
        return TokenSource(
            api_key=api_key,
            token_provider=provider,
            mint_api=baseten.client.managementapi.ApiClient(
                httpx.Client(transport=httpx.MockTransport(self.handle)),
                base_url="https://api.test",
                headers={"Authorization": f"Bearer {api_key}"},
            ),
        )


class TestTokenSource:
    def test_mints_with_api_key_and_reuses_token(self) -> None:
        server = _Server()
        tokens = server.tokens()
        assert tokens.token() == "token-1"
        assert tokens.token() == "token-1"
        assert len(server.mints) == 1
        assert server.mints[0].headers["Authorization"] == "Bearer key"
        assert json.loads(server.mints[0].content) == {"scopes": ["sandboxes"]}

    def test_refreshes_within_a_minute_of_expiry(self) -> None:
        server = _Server(expires_in=timedelta(seconds=30))
        tokens = server.tokens()
        assert tokens.token() == "token-1"
        assert tokens.token() == "token-2"

    def test_shares_one_mint_across_threads(self) -> None:
        release = threading.Event()
        server = _Server()
        server.mint_gate = release
        tokens = server.tokens()
        results: list[str | None] = []
        started = [threading.Event() for _ in range(5)]

        def get(started_event: threading.Event) -> None:
            started_event.set()
            results.append(tokens.token())

        threads = [threading.Thread(target=get, args=(event,)) for event in started]
        for thread in threads:
            thread.start()
        for event in started:
            event.wait()
        release.set()
        for thread in threads:
            thread.join()
        assert results == ["token-1"] * 5
        assert len(server.mints) == 1

    def test_mints_again_after_failed_mint(self) -> None:
        server = _Server()
        server.mint_failures = 1
        tokens = server.tokens()
        with pytest.raises(baseten.client.managementapi.ResponseError):
            tokens.token()
        assert tokens.token() == "token-2"

    def test_invalidate_drops_only_matching_token(self) -> None:
        server = _Server()
        tokens = server.tokens()
        assert tokens.token() == "token-1"
        tokens.invalidate("older-token")
        assert tokens.token() == "token-1"
        tokens.invalidate("token-1")
        assert tokens.token() == "token-2"

    def test_calls_provider_on_every_request(self) -> None:
        contexts: list[SandboxTokenProviderContext] = []

        def provider(context: SandboxTokenProviderContext) -> str:
            contexts.append(context)
            return f"provided-{len(contexts)}"

        tokens = _Server().tokens(api_key="", provider=provider)
        assert tokens.token() == "provided-1"
        assert tokens.token(revoked_token="provided-1") == "provided-2"
        assert [context.revoked_token for context in contexts] == [None, "provided-1"]

    def test_rejects_api_key_alongside_provider(self) -> None:
        with pytest.raises(ValueError, match="api_key must be empty"):
            _Server().tokens(provider=lambda context: "token")

    def test_no_token_for_empty_api_key_without_provider(self) -> None:
        server = _Server()
        assert server.tokens(api_key="").token() is None
        assert server.mints == []


class TestAsyncTokenSource:
    @pytest.mark.asyncio
    async def test_shares_one_mint_across_tasks(self) -> None:
        release = asyncio.Event()
        server = _Server()
        tokens = _async_tokens(server, release=release)
        tasks = [asyncio.create_task(tokens.token()) for _ in range(5)]
        # Lets every task reach the shared mint before it completes.
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*tasks) == ["token-1"] * 5
        assert len(server.mints) == 1

    @pytest.mark.asyncio
    async def test_cancelled_caller_does_not_fail_shared_mint(self) -> None:
        release = asyncio.Event()
        server = _Server()
        tokens = _async_tokens(server, release=release)
        cancelled = asyncio.create_task(tokens.token())
        waiting = asyncio.create_task(tokens.token())
        await asyncio.sleep(0)
        cancelled.cancel()
        release.set()
        assert await waiting == "token-1"
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert len(server.mints) == 1

    @pytest.mark.asyncio
    async def test_mints_again_after_failed_mint(self) -> None:
        server = _Server()
        server.mint_failures = 1
        tokens = _async_tokens(server)
        with pytest.raises(baseten.client.managementapi.ResponseError):
            await tokens.token()
        assert await tokens.token() == "token-2"

    @pytest.mark.asyncio
    async def test_calls_provider_with_revoked_token(self) -> None:
        contexts: list[SandboxTokenProviderContext] = []

        async def provider(context: SandboxTokenProviderContext) -> str:
            contexts.append(context)
            return "provided"

        tokens = _async_tokens(_Server(), api_key="", provider=provider)
        assert await tokens.token(revoked_token="old") == "provided"
        assert contexts[0].revoked_token == "old"


class TestTokenAuth:
    def test_sends_bearer_and_keeps_other_headers(self) -> None:
        server = _Server()
        _get(server, server.tokens(), headers={"X-Other": "kept"})
        assert server.requests[0].headers["Authorization"] == "Bearer token-1"
        assert server.requests[0].headers["X-Other"] == "kept"

    def test_resends_with_new_token_after_revocation(self) -> None:
        server = _Server()
        server.respond = lambda request: (
            _revoked() if len(server.requests) == 1 else httpx.Response(200)
        )
        response = _get(server, server.tokens())
        assert response.status_code == 200
        assert server.authorizations == [
            "Bearer token-1",
            "Bearer token-2",
        ]

    def test_tells_caching_provider_which_token_was_revoked(self) -> None:
        server = _Server()
        server.respond = lambda request: (
            _revoked() if len(server.requests) == 1 else httpx.Response(200)
        )
        contexts: list[SandboxTokenProviderContext] = []

        def provider(context: SandboxTokenProviderContext) -> str:
            contexts.append(context)
            return f"provided-{len(contexts)}"

        _get(server, server.tokens(api_key="", provider=provider))
        assert [context.revoked_token for context in contexts] == [None, "provided-1"]

    def test_returns_revocation_after_two_resends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        monkeypatch.setattr(
            baseten.sandbox._auth.time, "sleep", lambda seconds: delays.append(seconds)
        )
        server = _Server(respond=_revoked)
        response = _get(server, server.tokens())
        assert response.status_code == 401
        assert len(server.requests) == 3
        # Only the second resend waits, to be minted past the revocation.
        assert delays == [baseten.sandbox._auth._token_revoked_retry_delay]

    def test_does_not_resend_other_401(self) -> None:
        server = _Server(respond=lambda request: httpx.Response(401))
        assert _get(server, server.tokens()).status_code == 401
        assert len(server.requests) == 1

    def test_does_not_resend_stream_body(self) -> None:
        server = _Server(respond=_revoked)
        with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
            response = client.post(
                "https://sbx.test/upload",
                content=iter([b"chunk"]),
                auth=TokenAuth(server.tokens()),
            )
        assert response.status_code == 401
        assert len(server.requests) == 1

    def test_sends_no_authorization_without_token(self) -> None:
        server = _Server()
        _get(server, server.tokens(api_key=""))
        assert "Authorization" not in server.requests[0].headers


class TestAsyncTokenAuth:
    @pytest.mark.asyncio
    async def test_resends_with_new_token_after_revocation(self) -> None:
        server = _Server()
        server.respond = lambda request: (
            _revoked() if len(server.requests) == 1 else httpx.Response(200)
        )
        tokens = _async_tokens(server)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(server.handle)
        ) as client:
            response = await client.get(
                "https://sbx.test/x", auth=AsyncTokenAuth(tokens)
            )
        assert response.status_code == 200
        assert server.authorizations == [
            "Bearer token-1",
            "Bearer token-2",
        ]

    @pytest.mark.asyncio
    async def test_does_not_resend_stream_body(self) -> None:
        server = _Server(respond=_revoked)

        async def body() -> AsyncIterator[bytes]:
            yield b"chunk"

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(server.handle)
        ) as client:
            response = await client.post(
                "https://sbx.test/upload",
                content=body(),
                auth=AsyncTokenAuth(_async_tokens(server)),
            )
        assert response.status_code == 401
        assert len(server.requests) == 1


def _get(
    server: _Server, tokens: TokenSource, headers: dict[str, str] | None = None
) -> httpx.Response:
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        return client.get("https://sbx.test/x", headers=headers, auth=TokenAuth(tokens))


def _async_tokens(
    server: _Server,
    *,
    api_key: str = "key",
    provider=None,
    release: asyncio.Event | None = None,
) -> AsyncTokenSource:
    async def handle(request: httpx.Request) -> httpx.Response:
        if release is not None and request.url.path == "/v1/token":
            await release.wait()
        return server.handle(request)

    return AsyncTokenSource(
        api_key=api_key,
        token_provider=provider,
        mint_api=baseten.client.managementapi.AsyncApiClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            base_url="https://api.test",
            headers={"Authorization": f"Bearer {api_key}"},
        ),
    )

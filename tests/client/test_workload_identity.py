from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from baseten.client import (
    AccessToken,
    AccessTokenOptions,
    AsyncInferenceClient,
    AsyncManagementClient,
    AsyncWorkloadIdentityCredentials,
    InferenceClient,
    ManagementClient,
    WorkloadIdentityCredentials,
    _workload_identity,
)
from baseten.client.managementapi import ResponseError

_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_BASE_URL = "https://api.test"
_ORG = {
    "org_id": "o",
    "created_at": "2026-01-01T00:00:00Z",
    "allow_personal_api_keys": True,
    "allow_service_account_api_keys": True,
}
_OPTIONS = AccessTokenOptions()
_CONTEXT = "while exchanging an identity token under trust rule rule-1"


class _Server:
    """Stands in for ``POST /v1/auth/token``, issuing ``access-<n>``.

    Other requests record their Authorization and get *respond*'s response.
    """

    def __init__(self, *, expires_in: int = 3600) -> None:
        self.expires_in = expires_in
        self.assertions: list[str] = []
        self.exchange_response: Callable[[], httpx.Response] | None = None
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda request: (
            httpx.Response(200, json=_ORG)
        )
        self.authorizations: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/auth/token":
            self.authorizations.append(request.headers.get("Authorization"))
            return self.respond(request)
        assert request.method == "POST"
        assert request.headers["Content-Type"] == "application/json"
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body.keys() == {
            "grant_type",
            "subject_token",
            "subject_token_type",
            "audience",
        }
        assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        assert body["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
        assert body["audience"] == "rule-1"
        self.assertions.append(body["subject_token"])
        if self.exchange_response is not None:
            return self.exchange_response()
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{len(self.assertions)}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "issued_token_type": _ACCESS_TOKEN_TYPE,
            },
        )

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def credentials(self, **kwargs) -> WorkloadIdentityCredentials:
        kwargs.setdefault("trust_rule_id", "rule-1")
        if "identity_token_file" not in kwargs:
            kwargs.setdefault("identity_token", lambda: "idp-jwt")
        return WorkloadIdentityCredentials(
            base_url_override=_BASE_URL,
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
            **kwargs,
        )

    def async_credentials(self, **kwargs) -> AsyncWorkloadIdentityCredentials:
        kwargs.setdefault("trust_rule_id", "rule-1")

        async def identity_token() -> str:
            return "idp-jwt"

        if "identity_token_file" not in kwargs:
            kwargs.setdefault("identity_token", identity_token)
        return AsyncWorkloadIdentityCredentials(
            base_url_override=_BASE_URL,
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(self.handle_async)
            ),
            **kwargs,
        )


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _with_clock(credentials):
    clock = _Clock()
    credentials._now = clock
    return clock


def _error_response(status: int, body: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, text=body)


class TestWorkloadIdentityCredentials:
    def test_exchanges_and_caches(self) -> None:
        server = _Server()
        credentials = server.credentials()
        before = datetime.now(UTC)
        token = credentials.access_token(_OPTIONS)
        assert token.token == "access-1"
        assert token.expires_at >= before + timedelta(hours=1)
        assert credentials.access_token(_OPTIONS).token == "access-1"
        assert server.assertions == ["idp-jwt"]

    def test_refreshes_within_leeway(self) -> None:
        server = _Server(expires_in=600)
        credentials = server.credentials(refresh_leeway=timedelta(minutes=1))
        clock = _with_clock(credentials)
        credentials.access_token(_OPTIONS)
        clock.now += timedelta(minutes=8, seconds=59)
        assert credentials.access_token(_OPTIONS).token == "access-1"
        clock.now += timedelta(seconds=1)
        assert credentials.access_token(_OPTIONS).token == "access-2"

    def test_leeway_capped_at_half_lifetime(self) -> None:
        # The default five-minute leeway exceeds the lifetime; without the cap
        # every call would exchange.
        server = _Server(expires_in=60)
        credentials = server.credentials()
        clock = _with_clock(credentials)
        credentials.access_token(_OPTIONS)
        clock.now += timedelta(seconds=29)
        credentials.access_token(_OPTIONS)
        assert len(server.assertions) == 1
        clock.now += timedelta(seconds=1)
        credentials.access_token(_OPTIONS)
        assert len(server.assertions) == 2

    def test_rereads_token_file_on_every_exchange(self, tmp_path: Path) -> None:
        server = _Server(expires_in=600)
        path = tmp_path / "token"
        path.write_text("jwt-one\n")
        credentials = server.credentials(identity_token_file=path)
        clock = _with_clock(credentials)
        credentials.access_token(_OPTIONS)
        path.write_text("jwt-two")
        clock.now += timedelta(minutes=10)
        credentials.access_token(_OPTIONS)
        assert server.assertions == ["jwt-one", "jwt-two"]

    def test_missing_token_file(self, tmp_path: Path) -> None:
        server = _Server()
        credentials = server.credentials(identity_token_file=tmp_path / "missing")
        with pytest.raises(FileNotFoundError):
            credentials.access_token(_OPTIONS)
        assert server.assertions == []

    def test_empty_token_file(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("\n")
        credentials = _Server().credentials(identity_token_file=path)
        with pytest.raises(ValueError, match="is empty"):
            credentials.access_token(_OPTIONS)

    def test_invalid_grant_is_explained_and_not_cached(self) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(
            400, json={"error": "invalid_grant"}
        )
        credentials = server.credentials()
        with pytest.raises(ResponseError) as exc_info:
            credentials.access_token(_OPTIONS)
        assert exc_info.value.status_code == 400
        assert exc_info.value.body == '{"error":"invalid_grant"}'
        context, hint = exc_info.value.__notes__
        assert context == _CONTEXT
        assert "trust rule rule-1" in hint
        assert "may have expired" in hint
        with pytest.raises(ResponseError):
            credentials.access_token(_OPTIONS)
        assert len(server.assertions) == 2

    def test_non_json_error_keeps_whole_body(self) -> None:
        body = "<html>" + "x" * 2000 + "</html>"
        server = _Server()
        server.exchange_response = _error_response(502, body)
        with pytest.raises(ResponseError) as exc_info:
            server.credentials().access_token(_OPTIONS)
        assert (exc_info.value.status_code, exc_info.value.body) == (502, body)
        assert exc_info.value.__notes__ == [_CONTEXT]

    def test_other_error_status(self) -> None:
        server = _Server()
        server.exchange_response = _error_response(429, '{"error":"rate_limited"}')
        with pytest.raises(ResponseError, match="HTTP 429") as exc_info:
            server.credentials().access_token(_OPTIONS)
        assert exc_info.value.__notes__ == [_CONTEXT]

    def test_identity_token_errors_note_the_trust_rule(self) -> None:
        def identity_token() -> str:
            raise OSError("idp unreachable")

        with pytest.raises(OSError, match="idp unreachable") as exc_info:
            _Server().credentials(identity_token=identity_token).access_token(_OPTIONS)
        assert exc_info.value.__notes__ == [_CONTEXT]

    def test_ignores_the_http_clients_own_auth(self) -> None:
        class Leaky(httpx.Auth):
            def auth_flow(self, request):
                request.headers["Authorization"] = "Bearer other-client"
                yield request

        server = _Server()
        credentials = WorkloadIdentityCredentials(
            trust_rule_id="rule-1",
            identity_token=lambda: "idp-jwt",
            base_url_override=_BASE_URL,
            http_client_override=httpx.Client(
                auth=Leaky(), transport=httpx.MockTransport(server.handle)
            ),
        )
        assert credentials.access_token(_OPTIONS).token == "access-1"

    def test_does_not_follow_redirects(self) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(
            307, headers={"Location": "https://elsewhere.test/v1/auth/token"}
        )
        credentials = WorkloadIdentityCredentials(
            trust_rule_id="rule-1",
            identity_token=lambda: "idp-jwt",
            base_url_override=_BASE_URL,
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(server.handle)
            ),
        )
        with pytest.raises(ResponseError, match="HTTP 307"):
            credentials.access_token(_OPTIONS)
        assert server.assertions == ["idp-jwt"]

    def test_exchange_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_workload_identity, "_EXCHANGE_TIMEOUT", httpx.Timeout(0.1))
        # Accepts the connection but never answers it.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host, port = listener.getsockname()
            credentials = WorkloadIdentityCredentials(
                trust_rule_id="rule-1",
                identity_token=lambda: "idp-jwt",
                base_url_override=f"http://{host}:{port}",
            )
            with pytest.raises(httpx.TimeoutException):
                credentials.access_token(_OPTIONS)

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (
                {"access_token": "", "token_type": "Bearer", "expires_in": 60},
                "no access_token",
            ),
            (
                {"access_token": "a", "token_type": "MAC", "expires_in": 60},
                "unsupported token_type",
            ),
            (
                {"access_token": "a", "token_type": "Bearer", "expires_in": 0},
                "invalid expires_in",
            ),
        ],
    )
    def test_malformed_success(self, body: dict[str, object], match: str) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(
            200, json={**body, "issued_token_type": _ACCESS_TOKEN_TYPE}
        )
        with pytest.raises(ValueError, match=match):
            server.credentials().access_token(_OPTIONS)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"trust_rule_id": "r"},
            {"trust_rule_id": "r", "identity_token_file": "/f", "identity_token": str},
            {"trust_rule_id": "", "identity_token": str},
            {"trust_rule_id": "r", "identity_token_file": ""},
            {
                "trust_rule_id": "r",
                "identity_token": str,
                "refresh_leeway": timedelta(seconds=-1),
            },
        ],
    )
    def test_validates_options(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            WorkloadIdentityCredentials(**kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


class TestAsyncWorkloadIdentityCredentials:
    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_exchange(self) -> None:
        server = _Server()
        release = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            await release.wait()
            return server.handle(request)

        credentials = AsyncWorkloadIdentityCredentials(
            trust_rule_id="rule-1",
            identity_token=server.async_credentials()._identity_token,
            base_url_override=_BASE_URL,
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )
        tasks = [
            asyncio.create_task(credentials.access_token(_OPTIONS)) for _ in range(32)
        ]
        await asyncio.sleep(0)
        release.set()
        tokens = await asyncio.gather(*tasks)
        assert {token.token for token in tokens} == {"access-1"}
        assert len(server.assertions) == 1

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_cancel_exchange(self) -> None:
        server = _Server()
        release = asyncio.Event()

        async def identity_token() -> str:
            await release.wait()
            return "idp-jwt"

        credentials = server.async_credentials(identity_token=identity_token)
        cancelled = asyncio.create_task(credentials.access_token(_OPTIONS))
        waiting = asyncio.create_task(credentials.access_token(_OPTIONS))
        await asyncio.sleep(0)
        cancelled.cancel()
        release.set()
        assert (await waiting).token == "access-1"
        with pytest.raises(asyncio.CancelledError):
            await cancelled

    @pytest.mark.asyncio
    async def test_invalid_grant_is_explained(self) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(
            400, json={"error": "invalid_grant"}
        )
        with pytest.raises(ResponseError) as exc_info:
            await server.async_credentials().access_token(_OPTIONS)
        assert exc_info.value.status_code == 400
        context, hint = exc_info.value.__notes__
        assert context == _CONTEXT
        assert "trust rule rule-1" in hint

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("response", "match"),
        [
            (httpx.Response(200, text="<html>"), "unexpected content type"),
            (httpx.Response(200, json=["a"]), "validation error"),
            (
                httpx.Response(
                    200,
                    json={
                        "access_token": "a",
                        "token_type": "Bearer",
                        "expires_in": 0,
                        "issued_token_type": _ACCESS_TOKEN_TYPE,
                    },
                ),
                "invalid expires_in",
            ),
        ],
    )
    async def test_malformed_success(
        self, response: httpx.Response, match: str
    ) -> None:
        server = _Server()
        server.exchange_response = lambda: response
        with pytest.raises(ValueError, match=match):
            await server.async_credentials().access_token(_OPTIONS)

    @pytest.mark.asyncio
    async def test_rereads_token_file(self, tmp_path: Path) -> None:
        server = _Server(expires_in=600)
        path = tmp_path / "token"
        path.write_text("jwt-one")
        credentials = server.async_credentials(identity_token_file=path)
        clock = _with_clock(credentials)
        await credentials.access_token(_OPTIONS)
        path.write_text("jwt-two")
        clock.now += timedelta(minutes=10)
        await credentials.access_token(_OPTIONS)
        assert server.assertions == ["jwt-one", "jwt-two"]


def _management(server: _Server, credentials) -> ManagementClient:
    client = ManagementClient(credentials=credentials, base_url_override=_BASE_URL)
    client.http_client._transport = httpx.MockTransport(server.handle)  # type: ignore[attr-defined]
    return client


class TestClientsWithCredentials:
    def test_send_the_current_token(self) -> None:
        server = _Server(expires_in=600)
        credentials = server.credentials()
        clock = _with_clock(credentials)
        management = _management(server, credentials)
        management.api.get_organizations_me()
        clock.now += timedelta(hours=1)
        management.api.get_organizations_me()
        inference = InferenceClient(
            credentials=credentials, base_url_override=_BASE_URL
        )
        inference.http_client._transport = httpx.MockTransport(server.handle)  # type: ignore[attr-defined]
        inference.http_client.get("/anything")
        assert server.authorizations == [
            "Bearer access-1",
            "Bearer access-2",
            "Bearer access-2",
        ]

    @pytest.mark.asyncio
    async def test_async_clients_send_the_current_token(self) -> None:
        server = _Server()
        credentials = server.async_credentials()
        management = AsyncManagementClient(
            credentials=credentials, base_url_override=_BASE_URL
        )
        management.http_client._transport = httpx.MockTransport(server.handle_async)  # type: ignore[attr-defined]
        await management.api.get_organizations_me()
        inference = AsyncInferenceClient(
            credentials=credentials, base_url_override=_BASE_URL
        )
        inference.http_client._transport = httpx.MockTransport(server.handle_async)  # type: ignore[attr-defined]
        await inference.http_client.get("/anything")
        assert server.authorizations == ["Bearer access-1", "Bearer access-1"]

    @pytest.mark.parametrize(
        ("challenge", "second"),
        [
            ('Bearer realm="baseten", error="invalid_token"', "Bearer access-2"),
            ('Bearer realm="model", error="invalid_token"', "Bearer access-1"),
            ('Bearer realm="baseten"', "Bearer access-1"),
            (None, "Bearer access-1"),
        ],
        ids=[
            "baseten_refusal",
            "other_realm",
            "authentication_required",
            "no_challenge",
        ],
    )
    def test_sends_each_request_once(self, challenge: str | None, second: str) -> None:
        # Only a refusal from Baseten's auth layer makes the next request use a
        # new token; the refused request itself is the caller's to see.
        server = _Server()
        server.respond = _refuse_once(challenge)
        management = _management(server, server.credentials())
        with pytest.raises(ResponseError, match="HTTP 401"):
            management.api.get_organizations_me()
        management.api.get_organizations_me()
        assert server.authorizations == ["Bearer access-1", second]

    @pytest.mark.asyncio
    async def test_async_replaces_token_baseten_refused(self) -> None:
        server = _Server()
        server.respond = _refuse_once('Bearer realm="baseten", error="invalid_token"')
        inference = AsyncInferenceClient(
            credentials=server.async_credentials(), base_url_override=_BASE_URL
        )
        inference.http_client._transport = httpx.MockTransport(server.handle_async)  # type: ignore[attr-defined]
        response = await inference.http_client.get("/predict")
        assert response.status_code == 401
        await inference.http_client.get("/predict")
        assert server.authorizations == ["Bearer access-1", "Bearer access-2"]

    def test_accepts_own_credentials(self) -> None:
        seen: list[AccessTokenOptions] = []

        class Own:
            def access_token(self, options: AccessTokenOptions) -> AccessToken:
                seen.append(options)
                return AccessToken(
                    token="own", expires_at=datetime.now(UTC) + timedelta(hours=1)
                )

        server = _Server()
        _management(server, Own()).api.get_organizations_me()
        assert server.authorizations == ["Bearer own"]
        assert seen == [AccessTokenOptions()]

    def test_leaves_other_hosts_alone(self) -> None:
        server = _Server()
        management = _management(server, server.credentials())
        management.http_client.put("https://storage.test/presigned")
        management.http_client.get(
            "/explicit", headers={"Authorization": "Bearer mine"}
        )
        assert server.authorizations == [None, "Bearer mine"]
        assert server.assertions == []

    def test_redirect_to_other_host_drops_token(self) -> None:
        server = _Server()
        server.respond = lambda request: (
            httpx.Response(302, headers={"Location": "https://sub.api.test/landing"})
            if request.url.path == "/start"
            else httpx.Response(200)
        )
        management = _management(server, server.credentials())
        management.http_client.get("/start", follow_redirects=True)
        assert server.authorizations == ["Bearer access-1", None]

    def test_management_with_http_client_override(self) -> None:
        server = _Server()
        http_client = httpx.Client(
            base_url=_BASE_URL, transport=httpx.MockTransport(server.handle)
        )
        # No base_url_override: the token goes where the override sends
        # requests, not to the default base URL.
        management = ManagementClient(
            credentials=server.credentials(), http_client_override=http_client
        )
        management.api.get_organizations_me()
        # Only the API client authenticates; the HTTP client is the caller's.
        management.http_client.get("/direct")
        assert server.authorizations == ["Bearer access-1", None]

    def test_inference_with_http_client_override(self) -> None:
        server = _Server()
        server.respond = lambda request: httpx.Response(202)
        http_client = httpx.Client(
            base_url="https://model-x.api.test",
            transport=httpx.MockTransport(server.handle),
        )
        # Neither model_id nor chain_id, which only the default URL needs.
        inference = InferenceClient(
            credentials=server.credentials(), http_client_override=http_client
        )
        inference.api.wake(env_name="production")
        assert server.authorizations == ["Bearer access-1"]

    def test_validates_auth_options(self) -> None:
        credentials = _Server().credentials()
        with pytest.raises(ValueError, match="mutually exclusive"):
            ManagementClient(api_key="k", credentials=credentials)
        # An empty api_key counts as unset, as in Go.
        ManagementClient(api_key="", credentials=credentials).close()
        with pytest.raises(ValueError, match="required"):
            ManagementClient()
        for name in ["Authorization", "authorization"]:
            with pytest.raises(ValueError, match="Authorization"):
                ManagementClient(credentials=credentials, headers={name: "Bearer x"})
        with pytest.raises(ValueError, match="mutually exclusive"):
            InferenceClient(api_key="k", credentials=credentials, model_id="m")
        InferenceClient(credentials=credentials, model_id="m").close()
        with pytest.raises(TypeError, match="async"):
            AsyncManagementClient(credentials=credentials)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="async"):
            ManagementClient(credentials=_Server().async_credentials())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def _refuse_once(
    challenge: str | None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the first request with a 401 carrying *challenge*, then succeed."""
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {} if challenge is None else {"WWW-Authenticate": challenge}
            return httpx.Response(401, headers=headers)
        return httpx.Response(200, json=_ORG)

    return respond

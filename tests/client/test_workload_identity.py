from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import baseten.client._token_auth
import baseten.client.managementapi
from baseten.client import (
    AccessToken,
    AsyncInferenceClient,
    AsyncManagementClient,
    AsyncWorkloadIdentityCredentials,
    InferenceClient,
    InvalidGrantError,
    ManagementClient,
    WorkloadIdentityCredentials,
    WorkloadIdentityError,
)

_BASE_URL = "https://api.test"
_ORG = {
    "org_id": "o",
    "created_at": "2026-01-01T00:00:00Z",
    "allow_personal_api_keys": True,
    "allow_service_account_api_keys": True,
}


@pytest.fixture(autouse=True)
def no_resend_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baseten.client._token_auth, "_resend_delay_sec", 0.0)


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
        token = credentials.access_token()
        assert token.token == "access-1"
        assert token.expires_at >= before + timedelta(hours=1)
        assert credentials.access_token().token == "access-1"
        assert server.assertions == ["idp-jwt"]

    def test_refreshes_within_leeway(self) -> None:
        server = _Server(expires_in=600)
        credentials = server.credentials(refresh_before_expiry=timedelta(minutes=1))
        clock = _with_clock(credentials)
        credentials.access_token()
        clock.now += timedelta(minutes=8, seconds=59)
        assert credentials.access_token().token == "access-1"
        clock.now += timedelta(seconds=1)
        assert credentials.access_token().token == "access-2"

    def test_leeway_capped_at_half_lifetime(self) -> None:
        # The default five-minute leeway exceeds the lifetime; without the cap
        # every call would exchange.
        server = _Server(expires_in=60)
        credentials = server.credentials()
        clock = _with_clock(credentials)
        credentials.access_token()
        clock.now += timedelta(seconds=29)
        credentials.access_token()
        assert len(server.assertions) == 1
        clock.now += timedelta(seconds=1)
        credentials.access_token()
        assert len(server.assertions) == 2

    def test_rereads_token_file_on_every_exchange(self, tmp_path: Path) -> None:
        server = _Server(expires_in=600)
        path = tmp_path / "token"
        path.write_text("jwt-one\n")
        credentials = server.credentials(identity_token_file=path)
        clock = _with_clock(credentials)
        credentials.access_token()
        path.write_text("jwt-two")
        clock.now += timedelta(minutes=10)
        credentials.access_token()
        assert server.assertions == ["jwt-one", "jwt-two"]

    def test_missing_token_file(self, tmp_path: Path) -> None:
        server = _Server()
        credentials = server.credentials(identity_token_file=tmp_path / "missing")
        with pytest.raises(FileNotFoundError):
            credentials.access_token()
        assert server.assertions == []

    def test_empty_token_file(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("\n")
        credentials = _Server().credentials(identity_token_file=path)
        with pytest.raises(WorkloadIdentityError, match="is empty"):
            credentials.access_token()

    def test_invalid_grant_is_explained_and_not_cached(self) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(
            400, json={"error": "invalid_grant"}
        )
        credentials = server.credentials()
        with pytest.raises(InvalidGrantError, match="rule-1"):
            credentials.access_token()
        with pytest.raises(InvalidGrantError):
            credentials.access_token()
        assert len(server.assertions) == 2

    def test_non_json_response(self) -> None:
        server = _Server()
        server.exchange_response = _error_response(502, "<html>bad gateway</html>")
        with pytest.raises(
            WorkloadIdentityError,
            match="HTTP 502 with a non-JSON response.*bad gateway",
        ):
            server.credentials().access_token()

    def test_other_error_status(self) -> None:
        server = _Server()
        server.exchange_response = _error_response(429, '{"error":"rate_limited"}')
        with pytest.raises(WorkloadIdentityError, match="HTTP 429") as exc_info:
            server.credentials().access_token()
        assert not isinstance(exc_info.value, InvalidGrantError)

    @pytest.mark.parametrize(
        "body",
        [
            {"token_type": "Bearer", "expires_in": 60},
            {"access_token": "a", "token_type": "MAC", "expires_in": 60},
            {"access_token": "a", "token_type": "Bearer"},
        ],
    )
    def test_malformed_success(self, body: dict[str, object]) -> None:
        server = _Server()
        server.exchange_response = lambda: httpx.Response(200, json=body)
        with pytest.raises(WorkloadIdentityError):
            server.credentials().access_token()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"trust_rule_id": "r"},
            {"trust_rule_id": "r", "identity_token_file": "/f", "identity_token": str},
            {"trust_rule_id": "", "identity_token": str},
            {
                "trust_rule_id": "r",
                "identity_token": str,
                "refresh_before_expiry": timedelta(seconds=-1),
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
        tasks = [asyncio.create_task(credentials.access_token()) for _ in range(32)]
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
        cancelled = asyncio.create_task(credentials.access_token())
        waiting = asyncio.create_task(credentials.access_token())
        await asyncio.sleep(0)
        cancelled.cancel()
        release.set()
        assert (await waiting).token == "access-1"
        with pytest.raises(asyncio.CancelledError):
            await cancelled

    @pytest.mark.asyncio
    async def test_exchange_timeout(self) -> None:
        async def identity_token() -> str:
            await asyncio.Event().wait()
            return "never"

        credentials = _Server().async_credentials(
            identity_token=identity_token, exchange_timeout_sec=0.01
        )
        with pytest.raises(WorkloadIdentityError, match="timed out"):
            await credentials.access_token()

    @pytest.mark.asyncio
    async def test_rereads_token_file(self, tmp_path: Path) -> None:
        server = _Server(expires_in=600)
        path = tmp_path / "token"
        path.write_text("jwt-one")
        credentials = server.async_credentials(identity_token_file=path)
        clock = _with_clock(credentials)
        await credentials.access_token()
        path.write_text("jwt-two")
        clock.now += timedelta(minutes=10)
        await credentials.access_token()
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

    def test_resends_refused_token(self) -> None:
        server = _Server()
        server.respond = _refuse_access_1
        management = _management(server, server.credentials())
        management.api.get_organizations_me()
        assert server.authorizations == ["Bearer access-1", "Bearer access-2"]

    @pytest.mark.asyncio
    async def test_async_resends_refused_token(self) -> None:
        server = _Server()
        server.respond = _refuse_access_1
        management = AsyncManagementClient(
            credentials=server.async_credentials(), base_url_override=_BASE_URL
        )
        management.http_client._transport = httpx.MockTransport(server.handle_async)  # type: ignore[attr-defined]
        await management.api.get_organizations_me()
        assert server.authorizations == ["Bearer access-1", "Bearer access-2"]

    def test_sends_once_for_source_without_invalidation(self) -> None:
        class Source:
            def access_token(self) -> AccessToken:
                return AccessToken(
                    token="access-1", expires_at=datetime.now(UTC) + timedelta(hours=1)
                )

        server = _Server()
        server.respond = _refuse_access_1
        management = _management(server, Source())
        with pytest.raises(baseten.client.managementapi.ResponseError):
            management.api.get_organizations_me()
        assert server.authorizations == ["Bearer access-1"]

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

    def test_validates_auth_options(self) -> None:
        credentials = _Server().credentials()
        with pytest.raises(ValueError, match="mutually exclusive"):
            ManagementClient(api_key="k", credentials=credentials)
        with pytest.raises(ValueError, match="mutually exclusive"):
            ManagementClient(api_key="", credentials=credentials)
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


def _refuse_access_1(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") == "Bearer access-1":
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer realm="baseten", error="invalid_token"'
            },
        )
    return httpx.Response(200, json=_ORG)

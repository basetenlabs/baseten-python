from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import baseten.client.managementapi
from baseten.sandbox import (
    SandboxEnvValue,
    SandboxExpirationPolicyDate,
    SandboxExpirationPolicyIdle,
    SandboxExpirationPolicyMaxAge,
    SandboxExpirationPolicyUnknown,
    SandboxLifecycle,
    SandboxNetwork,
    SandboxNetworkProxy,
    SandboxNetworkProxyRoute,
    SandboxPort,
)
from baseten.sandbox._info import (
    envs_to_api,
    lifecycle_to_api,
    network_to_api,
    ports_to_api,
    sandbox_info_from_api,
)

_FULL_RECORD: dict[str, Any] = {
    "name": "sbx",
    "url": "https://sbx.example.dev/",
    "status": "DEPLOYED",
    "image": "baseten/base-image:latest",
    "memory": 4096,
    "region": "us-was-1",
    "envs": [
        {"name": "PLAIN", "value": "shown", "secret": False},
        {"name": "SECRET", "value": "****", "secret": True},
        {"value": "nameless"},
    ],
    "labels": {"team": "infra"},
    "external_id": "ext-1",
    "lifecycle": {
        "expiration_policies": [
            {"type": "TTL_IDLE", "action": "DELETE", "value": "1h30m"},
            {"type": "TTL_MAX_AGE", "action": "DELETE", "value": "7d"},
            {"type": "DATE", "action": "DELETE", "value": "2026-10-07T12:00:00Z"},
            {"type": "ON_WEEKEND", "action": "ARCHIVE", "value": "sat"},
        ],
        "terminated_retention": "24h",
    },
    "ports": [{"target": 8080, "name": "http", "protocol": "HTTP"}],
    "network": {
        "subnet": "default",
        "proxy": {
            "allowed_domains": ["pypi.org"],
            "routing": [{"destinations": ["*"], "headers": {"X-A": "b"}}],
        },
    },
    "created_at": "2026-10-06T12:00:00Z",
    "updated_at": "2026-10-06T13:00:00Z",
    "created_by": "creator",
    "updated_by": "updater",
    "last_used_at": "2026-10-06T14:00:00Z",
    "expires_in": 3600,
}


def test_maps_sandbox_record() -> None:
    info = sandbox_info_from_api(
        baseten.client.managementapi.Sandbox.model_validate(_FULL_RECORD)
    )
    assert info.name == "sbx"
    assert info.url == "https://sbx.example.dev/"
    assert info.status == "DEPLOYED"
    assert (info.image, info.memory, info.region) == (
        "baseten/base-image:latest",
        4096,
        "us-was-1",
    )
    # An env without a name is left out.
    assert info.envs == {
        "PLAIN": SandboxEnvValue(value="shown", secret=False),
        "SECRET": SandboxEnvValue(value="****", secret=True),
    }
    assert info.labels == {"team": "infra"}
    assert info.external_id == "ext-1"
    assert info.lifecycle == SandboxLifecycle(
        expiration_policies=[
            SandboxExpirationPolicyIdle(
                after=timedelta(hours=1, minutes=30), action="DELETE"
            ),
            SandboxExpirationPolicyMaxAge(after=timedelta(days=7), action="DELETE"),
            SandboxExpirationPolicyDate(
                at=datetime(2026, 10, 7, 12, tzinfo=UTC), action="DELETE"
            ),
            SandboxExpirationPolicyUnknown(
                type="ON_WEEKEND",
                raw={"type": "ON_WEEKEND", "action": "ARCHIVE", "value": "sat"},
            ),
        ],
        terminated_retention=timedelta(hours=24),
    )
    assert info.ports == [SandboxPort(target=8080, name="http", protocol="HTTP")]
    assert info.network == SandboxNetwork(
        subnet="default",
        proxy=SandboxNetworkProxy(
            allowed_domains=["pypi.org"],
            routing=[
                SandboxNetworkProxyRoute(destinations=["*"], headers={"X-A": "b"})
            ],
        ),
    )
    assert info.created_at == datetime(2026, 10, 6, 12, tzinfo=UTC)
    assert info.updated_at == datetime(2026, 10, 6, 13, tzinfo=UTC)
    assert (info.created_by, info.updated_by) == ("creator", "updater")
    assert info.last_used_at == datetime(2026, 10, 6, 14, tzinfo=UTC)
    assert info.expires_in == timedelta(hours=1)


def test_maps_sandbox_record_without_optional_fields() -> None:
    info = sandbox_info_from_api(
        baseten.client.managementapi.Sandbox.model_validate(
            {
                "name": "sbx",
                "url": "https://sbx.example.dev/",
                "status": "SOMETHING_NEW",
                "created_at": "2026-10-06T12:00:00Z",
            }
        )
    )
    assert info.status == "SOMETHING_NEW"
    assert info.envs == {}
    assert info.labels == {}
    assert info.lifecycle is None
    assert info.ports == []
    assert info.network is None
    assert info.expires_in is None


def test_lifecycle_to_api() -> None:
    lifecycle = lifecycle_to_api(
        SandboxLifecycle(
            expiration_policies=[
                SandboxExpirationPolicyIdle(after=timedelta(minutes=10)),
                SandboxExpirationPolicyMaxAge(
                    after=timedelta(days=1), action="ARCHIVE"
                ),
                SandboxExpirationPolicyDate(at=datetime(2026, 10, 7, tzinfo=UTC)),
                SandboxExpirationPolicyUnknown(
                    type="ON_WEEKEND", raw={"type": "ON_WEEKEND", "value": "sat"}
                ),
            ],
            terminated_retention=timedelta(hours=1),
        )
    )
    assert _dump(lifecycle) == {
        "expiration_policies": [
            {"type": "TTL_IDLE", "action": "DELETE", "value": "600000ms"},
            {"type": "TTL_MAX_AGE", "action": "ARCHIVE", "value": "86400000ms"},
            {"type": "DATE", "action": "DELETE", "value": "2026-10-07T00:00:00Z"},
            # Sent back exactly as the server sent it.
            {"type": "ON_WEEKEND", "value": "sat"},
        ],
        "terminated_retention": "3600000ms",
    }


def test_inputs_send_only_set_fields() -> None:
    assert _dump(lifecycle_to_api(SandboxLifecycle())) == {}
    assert [_dump(env) for env in envs_to_api({"A": SandboxEnvValue(value="1")})] == [
        {"name": "A", "value": "1"}
    ]
    assert [_dump(port) for port in ports_to_api([SandboxPort(target=80)])] == [
        {"target": 80}
    ]
    assert _dump(
        network_to_api(
            SandboxNetwork(
                proxy=SandboxNetworkProxy(
                    bypass=("example.com",),
                    routing=[SandboxNetworkProxyRoute(secrets={"k": "v"})],
                )
            )
        )
    ) == {"proxy": {"bypass": ["example.com"], "routing": [{"secrets": {"k": "v"}}]}}


def _dump(model: Any) -> Any:
    # As the generated client serializes a request body.
    return model.model_dump(mode="json", exclude_unset=True, by_alias=True)

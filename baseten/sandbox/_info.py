from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, TypeAlias

import baseten.client.managementapi
from baseten.sandbox._common import format_duration, parse_duration

SandboxStatus: TypeAlias = (
    Literal[
        "DEPLOYING",
        "DEPLOYED",
        "FAILED",
        "DEACTIVATING",
        "DEACTIVATED",
        "DELETING",
        "TERMINATED",
        "ARCHIVING",
        "ARCHIVED",
        "UNARCHIVING",
        "BUILDING",
        "UPLOADING",
    ]
    | str
)
"""Deployment status of a sandbox. Only ``DEPLOYED`` sandboxes run commands.

Other values may be added, so do not treat this list as exhaustive.
"""

SandboxExpirationAction: TypeAlias = Literal["DELETE"] | str
"""What an expiration policy does when met.

Other values may be added, so do not treat this list as exhaustive.
"""

SandboxPortProtocol: TypeAlias = Literal["HTTP", "TCP", "UDP", "TLS"] | str
"""Protocol of a sandbox port.

Other values may be added, so do not treat this list as exhaustive.
"""


@dataclass(kw_only=True)
class SandboxEnvValue:
    """Value of an environment variable in a sandbox."""

    value: str

    secret: bool | None = None
    """Whether the value is a secret. Defaults to true.

    Secret values come back masked as ``"****"``, unless read with
    ``show_secrets``.
    """


@dataclass(kw_only=True)
class SandboxExpirationPolicyIdle:
    """Expires a sandbox after a period without activity."""

    after: timedelta
    """How long the sandbox may go without activity."""

    action: SandboxExpirationAction | None = None
    """What happens when the policy is met. ``DELETE`` when unset."""


@dataclass(kw_only=True)
class SandboxExpirationPolicyMaxAge:
    """Expires a sandbox a period after its creation, regardless of activity."""

    after: timedelta
    """How long after creation the sandbox expires."""

    action: SandboxExpirationAction | None = None
    """What happens when the policy is met. ``DELETE`` when unset."""


@dataclass(kw_only=True)
class SandboxExpirationPolicyDate:
    """Expires a sandbox at a fixed time."""

    at: datetime
    """When the sandbox expires. Must be timezone-aware."""

    action: SandboxExpirationAction | None = None
    """What happens when the policy is met. ``DELETE`` when unset."""


@dataclass(kw_only=True)
class SandboxExpirationPolicyUnknown:
    """An expiration policy of a kind this SDK does not know.

    Kept as the server sent it, and sent back unchanged when given in a
    lifecycle, so updating a sandbox does not lose it.
    """

    type: str | None
    """The policy's type, when the server sent one as a string."""

    raw: dict[str, Any]
    """The policy exactly as the server sent it."""


SandboxExpirationPolicy: TypeAlias = (
    SandboxExpirationPolicyIdle
    | SandboxExpirationPolicyMaxAge
    | SandboxExpirationPolicyDate
    | SandboxExpirationPolicyUnknown
)
"""A condition for acting on a sandbox automatically.

Durations the server reports in days or weeks convert at 24 hours per day.
"""


@dataclass(kw_only=True)
class SandboxLifecycle:
    """When a sandbox is deleted automatically, and how long its record stays after."""

    expiration_policies: Sequence[SandboxExpirationPolicy] | None = None
    """Conditions for deleting the sandbox. Whichever is met first applies.

    Replaces all previous policies when updated.
    """

    terminated_retention: timedelta | None = None
    """How long the record stays after the sandbox terminates, for log access.

    The server defaults to 5 minutes.
    """


@dataclass(kw_only=True)
class SandboxPort:
    """A port the sandbox exposes."""

    target: int
    """Port number in the sandbox, 1 to 65535."""

    name: str | None = None

    protocol: SandboxPortProtocol | None = None


@dataclass(kw_only=True)
class SandboxNetworkProxyRoute:
    """A proxy rule injecting headers and body fields into requests to its destinations."""

    destinations: Sequence[str] | None = None
    """Destination domains the rule applies to. ``["*"]`` matches all."""

    headers: Mapping[str, str] | None = None
    """Headers to inject. Values may reference ``{{SECRET:name}}`` from ``secrets``."""

    body: Mapping[str, str] | None = None
    """Body fields to inject. Values may reference ``{{SECRET:name}}`` from ``secrets``."""

    secrets: Mapping[str, str] | None = None
    """Named secret values for this rule.

    Write-only, so always unset on :class:`SandboxInfo`.
    """


@dataclass(kw_only=True)
class SandboxNetworkProxy:
    """Proxy configuration of a sandbox's network."""

    allowed_domains: Sequence[str] | None = None
    """When set, only these external domains are reachable.

    Supports wildcards such as ``*.example.com``. Takes precedence over
    ``forbidden_domains``.
    """

    forbidden_domains: Sequence[str] | None = None
    """When set, every external domain except these is reachable. Supports wildcards."""

    bypass: Sequence[str] | None = None
    """Domains reached directly rather than through the proxy.

    Supports wildcards. Local and private addresses always bypass it.
    """

    routing: Sequence[SandboxNetworkProxyRoute] | None = None
    """Rules injecting headers and body fields into matching requests."""


@dataclass(kw_only=True)
class SandboxNetwork:
    """Network configuration of a sandbox, fixed at creation."""

    subnet: str | None = None
    """Subnet name. The server defaults to ``default``."""

    proxy: SandboxNetworkProxy | None = None
    """Routes the sandbox's HTTP traffic through the platform proxy."""


@dataclass(kw_only=True)
class SandboxInfo:
    """A sandbox as last reported by the control plane."""

    name: str
    """Unique name of the sandbox, assigned by the server when not given at creation."""

    url: str
    """Base URL of the sandbox's execution API."""

    status: SandboxStatus

    image: str | None
    """Image reference, including its tag."""

    memory: int | None
    """Memory in megabytes, which also sets the CPU allocation."""

    region: str | None

    envs: dict[str, SandboxEnvValue]
    """Environment variables, by name.

    Secret values are masked, so passing these back to an update overwrites
    their real values with the masks.
    """

    labels: dict[str, str]

    external_id: str | None
    """Caller-owned identifier for external lookups."""

    lifecycle: SandboxLifecycle | None

    ports: list[SandboxPort]

    network: SandboxNetwork | None

    created_at: datetime

    updated_at: datetime | None

    created_by: str | None

    updated_by: str | None

    last_used_at: datetime | None

    expires_in: timedelta | None
    """Time left before automatic deletion, when expiration is configured."""


def sandbox_info_from_api(sandbox: baseten.client.managementapi.Sandbox) -> SandboxInfo:
    """Convert the control plane's sandbox record."""
    return SandboxInfo(
        name=sandbox.name,
        url=str(sandbox.url),
        status=sandbox.status,
        image=sandbox.image,
        memory=sandbox.memory,
        region=sandbox.region,
        envs=_envs_from_api(sandbox.envs),
        labels=dict(sandbox.labels or {}),
        external_id=sandbox.external_id,
        lifecycle=None
        if sandbox.lifecycle is None
        else _lifecycle_from_api(sandbox.lifecycle),
        ports=ports_from_api(sandbox.ports),
        network=None if sandbox.network is None else _network_from_api(sandbox.network),
        created_at=sandbox.created_at,
        updated_at=sandbox.updated_at,
        created_by=sandbox.created_by,
        updated_by=sandbox.updated_by,
        last_used_at=sandbox.last_used_at,
        expires_in=None
        if sandbox.expires_in is None
        else timedelta(seconds=sandbox.expires_in),
    )


def ports_from_api(
    ports: Sequence[baseten.client.managementapi.SandboxPort] | None,
) -> list[SandboxPort]:
    """Convert the control plane's ports, ``None`` meaning none."""
    return [
        SandboxPort(target=port.target, name=port.name, protocol=port.protocol)
        for port in ports or []
    ]


def envs_to_api(
    envs: Mapping[str, SandboxEnvValue],
) -> list[baseten.client.managementapi.SandboxEnv]:
    """Convert environment variables to the control plane's form."""
    return [
        baseten.client.managementapi.SandboxEnv(
            **set_fields(name=name, value=env.value, secret=env.secret)
        )
        for name, env in envs.items()
    ]


def lifecycle_to_api(
    lifecycle: SandboxLifecycle,
) -> baseten.client.managementapi.SandboxLifecycle:
    """Convert a lifecycle to the control plane's form."""
    return baseten.client.managementapi.SandboxLifecycle(
        **set_fields(
            expiration_policies=None
            if lifecycle.expiration_policies is None
            else [_policy_to_api(policy) for policy in lifecycle.expiration_policies],
            terminated_retention=None
            if lifecycle.terminated_retention is None
            else format_duration(lifecycle.terminated_retention),
        )
    )


def ports_to_api(
    ports: Sequence[SandboxPort],
) -> list[baseten.client.managementapi.SandboxPort]:
    """Convert ports to the control plane's form."""
    return [
        baseten.client.managementapi.SandboxPort(
            **set_fields(target=port.target, name=port.name, protocol=port.protocol)
        )
        for port in ports
    ]


def network_to_api(
    network: SandboxNetwork,
) -> baseten.client.managementapi.SandboxNetwork:
    """Convert a network configuration to the control plane's form."""
    proxy = network.proxy
    return baseten.client.managementapi.SandboxNetwork(
        **set_fields(
            subnet=network.subnet,
            proxy=None
            if proxy is None
            else baseten.client.managementapi.SandboxProxyConfig(
                **set_fields(
                    allowed_domains=_optional_list(proxy.allowed_domains),
                    forbidden_domains=_optional_list(proxy.forbidden_domains),
                    bypass=_optional_list(proxy.bypass),
                    routing=None
                    if proxy.routing is None
                    else [
                        baseten.client.managementapi.SandboxProxyTarget(
                            **set_fields(
                                destinations=_optional_list(route.destinations),
                                headers=_optional_dict(route.headers),
                                body=_optional_dict(route.body),
                                secrets=_optional_dict(route.secrets),
                            )
                        )
                        for route in proxy.routing
                    ],
                )
            ),
        )
    )


def set_fields(**fields: Any) -> dict[str, Any]:
    """Return the fields that are not ``None``.

    Passing only these to a generated model leaves the rest unset, so they are
    left out of the request rather than sent as null or as the model's default.
    """
    return {name: value for name, value in fields.items() if value is not None}


def _policy_to_api(
    policy: SandboxExpirationPolicy,
) -> (
    baseten.client.managementapi.SandboxTTLIdleExpirationPolicy
    | baseten.client.managementapi.SandboxTTLMaxAgeExpirationPolicy
    | baseten.client.managementapi.SandboxDateExpirationPolicy
    | dict[str, Any]
):
    """Convert an expiration policy to the control plane's form."""
    if isinstance(policy, SandboxExpirationPolicyUnknown):
        return policy.raw
    action = "DELETE" if policy.action is None else policy.action
    match policy:
        case SandboxExpirationPolicyIdle():
            return baseten.client.managementapi.SandboxTTLIdleExpirationPolicy(
                type="TTL_IDLE", action=action, value=format_duration(policy.after)
            )
        case SandboxExpirationPolicyMaxAge():
            return baseten.client.managementapi.SandboxTTLMaxAgeExpirationPolicy(
                type="TTL_MAX_AGE", action=action, value=format_duration(policy.after)
            )
        case SandboxExpirationPolicyDate():
            return baseten.client.managementapi.SandboxDateExpirationPolicy(
                type="DATE", action=action, value=policy.at
            )


def _lifecycle_from_api(
    lifecycle: baseten.client.managementapi.SandboxLifecycle,
) -> SandboxLifecycle:
    retention = lifecycle.terminated_retention
    return SandboxLifecycle(
        expiration_policies=None
        if lifecycle.expiration_policies is None
        else [_policy_from_api(policy) for policy in lifecycle.expiration_policies],
        terminated_retention=None
        if retention is None
        else parse_duration(retention, "lifecycle terminated retention"),
    )


def _policy_from_api(
    policy: baseten.client.managementapi.SandboxTTLIdleExpirationPolicy
    | baseten.client.managementapi.SandboxTTLMaxAgeExpirationPolicy
    | baseten.client.managementapi.SandboxDateExpirationPolicy
    | dict[str, Any],
) -> SandboxExpirationPolicy:
    match policy:
        case baseten.client.managementapi.SandboxTTLIdleExpirationPolicy():
            return SandboxExpirationPolicyIdle(
                after=parse_duration(policy.value, "TTL_IDLE expiration policy"),
                action=policy.action,
            )
        case baseten.client.managementapi.SandboxTTLMaxAgeExpirationPolicy():
            return SandboxExpirationPolicyMaxAge(
                after=parse_duration(policy.value, "TTL_MAX_AGE expiration policy"),
                action=policy.action,
            )
        case baseten.client.managementapi.SandboxDateExpirationPolicy():
            return SandboxExpirationPolicyDate(at=policy.value, action=policy.action)
        case _:
            policy_type = policy.get("type")
            return SandboxExpirationPolicyUnknown(
                type=policy_type if isinstance(policy_type, str) else None,
                raw=dict(policy),
            )


def _network_from_api(
    network: baseten.client.managementapi.SandboxNetwork,
) -> SandboxNetwork:
    proxy = network.proxy
    return SandboxNetwork(
        subnet=network.subnet,
        proxy=None
        if proxy is None
        else SandboxNetworkProxy(
            allowed_domains=proxy.allowed_domains,
            forbidden_domains=proxy.forbidden_domains,
            bypass=proxy.bypass,
            routing=None
            if proxy.routing is None
            else [
                SandboxNetworkProxyRoute(
                    destinations=route.destinations,
                    headers=route.headers,
                    body=route.body,
                    secrets=route.secrets,
                )
                for route in proxy.routing
            ],
        ),
    )


def _envs_from_api(
    envs: list[baseten.client.managementapi.SandboxEnv] | None,
) -> dict[str, SandboxEnvValue]:
    result: dict[str, SandboxEnvValue] = {}
    for env in envs or []:
        if env.name is None:
            continue
        result[env.name] = SandboxEnvValue(
            value="" if env.value is None else env.value, secret=env.secret
        )
    return result


def _optional_list(values: Sequence[str] | None) -> list[str] | None:
    return None if values is None else list(values)


def _optional_dict(values: Mapping[str, str] | None) -> dict[str, str] | None:
    return None if values is None else dict(values)

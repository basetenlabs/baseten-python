from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

import baseten.client.managementapi
import baseten.client.sandboxapi

# Deployment status of a sandbox. Only DEPLOYED sandboxes run commands.
# Kept open: the server may add values, so do not treat this as exhaustive.
SANDBOX_STATUSES = (
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
)

# Whether a deployed sandbox is running or idle in standby. Kept open.
SANDBOX_STATES = ("RUNNING", "STANDBY")

# Status of a process in a sandbox. Kept open.
SANDBOX_PROCESS_STATUSES = ("running", "completed", "failed", "killed", "stopped")

SandboxStatus = str
SandboxState = str
SandboxProcessStatus = str


@dataclass
class SandboxEnvValue:
    """Value of an environment variable in a sandbox."""

    value: str
    secret: bool = False


@dataclass
class SandboxInfo:
    """A sandbox as last reported by the control plane."""

    name: str
    """Unique name of the sandbox, assigned by the server when not given."""

    status: SandboxStatus
    enabled: bool
    created_at: datetime
    envs: dict[str, SandboxEnvValue] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    url: str | None = None
    """Base URL of the sandbox's execution API, once it has one."""

    state: SandboxState | None = None
    image: str | None = None
    """Image reference, including its tag."""

    memory: int | None = None
    """Memory in megabytes, which also sets the CPU allocation."""

    region: str | None = None
    display_name: str | None = None
    external_id: str | None = None
    """Caller-owned identifier for external lookups."""

    updated_at: datetime | None = None
    created_by: str | None = None
    updated_by: str | None = None
    last_used_at: datetime | None = None
    expires_in_seconds: int | None = None
    """Seconds left before automatic deletion, when expiration is configured."""


@dataclass
class SandboxProcessInfo:
    """A process in a sandbox."""

    pid: str
    name: str
    command: str
    status: SandboxProcessStatus
    exit_code: int
    stdout: str
    stderr: str
    logs: str
    """Standard output and standard error, interleaved."""

    working_dir: str
    started_at: datetime
    completed_at: datetime | None = None


@dataclass
class LibraryImagePort:
    """One port a starter image's services listen on."""

    name: str | None = None
    target: int | None = None
    """Port number inside the sandbox."""

    protocol: str | None = None


@dataclass
class LibraryImage:
    """One starter image from the platform's starter-image library.

    Available to any sandbox without building or pushing; pass
    :attr:`image` as the image when creating a sandbox.
    """

    name: str
    """Stable identifier of the starter image."""

    image: str
    """Image reference, including its tag."""

    display_name: str | None = None
    description: str | None = None
    long_description: str | None = None
    memory: int | None = None
    """Default memory allocation in megabytes."""

    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    ports: list[LibraryImagePort] = field(default_factory=list)
    icon_url: str | None = None
    project_url: str | None = None
    """Project page for the image's stack."""

    enterprise: bool = False
    """Whether the image is gated to enterprise workspaces."""


def library_image_from_api(
    image: baseten.client.managementapi.SandboxLibraryImage,
) -> LibraryImage:
    """Convert the control plane's starter-image catalog entry."""
    return LibraryImage(
        name=image.name,
        image=image.image,
        display_name=image.displayName,
        description=image.description,
        long_description=image.longDescription,
        memory=image.memory,
        categories=image.categories or [],
        tags=image.tags or [],
        ports=[
            LibraryImagePort(name=port.name, target=port.target, protocol=port.protocol)
            for port in image.ports or []
        ],
        icon_url=image.icon,
        project_url=image.url,
        enterprise=bool(image.enterprise),
    )


def sandbox_info_from_api(sandbox: baseten.client.managementapi.Sandbox) -> SandboxInfo:
    """Convert the control plane's sandbox record."""
    return SandboxInfo(
        name=sandbox.name,
        url=str(sandbox.url) if sandbox.url is not None else None,
        status=str(sandbox.status),
        state=str(sandbox.state) if sandbox.state is not None else None,
        image=sandbox.image,
        memory=sandbox.memory,
        region=sandbox.region,
        enabled=sandbox.enabled,
        envs=_envs_from_api(sandbox.envs),
        labels=dict(sandbox.labels.root) if sandbox.labels is not None else {},
        display_name=sandbox.display_name,
        external_id=sandbox.external_id,
        created_at=sandbox.created_at,
        updated_at=sandbox.updated_at,
        created_by=sandbox.created_by,
        updated_by=sandbox.updated_by,
        last_used_at=sandbox.last_used_at,
        expires_in_seconds=sandbox.expires_in,
    )


def sandbox_envs_to_api(
    envs: dict[str, SandboxEnvValue],
) -> list[baseten.client.managementapi.SandboxEnv]:
    """Convert environment variables to the control plane's form."""
    return [
        baseten.client.managementapi.SandboxEnv(
            name=name, value=value.value, secret=value.secret
        )
        for name, value in envs.items()
    ]


def process_info_from_api(
    process: baseten.client.sandboxapi.ProcessResponse,
) -> SandboxProcessInfo:
    """Convert the execution plane's process record.

    The plane reports timestamps as strings in either ISO 8601 or RFC 1123
    form, and an empty completion time while the process runs.
    """
    return SandboxProcessInfo(
        pid=process.pid,
        name=process.name,
        command=process.command,
        status=str(process.status),
        exit_code=process.exitCode,
        stdout=process.stdout,
        stderr=process.stderr,
        logs=process.logs,
        working_dir=process.workingDir,
        started_at=parse_exec_timestamp(process.startedAt),
        completed_at=(
            parse_exec_timestamp(process.completedAt) if process.completedAt else None
        ),
    )


def parse_exec_timestamp(value: str) -> datetime:
    """Parse an execution-plane timestamp, ISO 8601 or RFC 1123."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    parsed = parsedate_to_datetime(value)
    if parsed is None:
        raise ValueError(f"unparseable execution-plane timestamp {value!r}") from None
    return parsed


def _envs_from_api(
    envs: list[baseten.client.managementapi.SandboxEnv] | None,
) -> dict[str, SandboxEnvValue]:
    result: dict[str, SandboxEnvValue] = {}
    for env in envs or []:
        if env.name is None:
            continue
        result[env.name] = SandboxEnvValue(
            value=env.value or "", secret=bool(env.secret)
        )
    return result

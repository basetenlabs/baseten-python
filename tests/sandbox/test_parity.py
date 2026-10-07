from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

import baseten.client.managementapi
import baseten.client.sandboxapi
from baseten.sandbox import (
    AsyncImageClient,
    AsyncSandbox,
    AsyncSandboxClient,
    AsyncSandboxCreateResult,
    AsyncSandboxFileSystem,
    AsyncSandboxProcess,
    AsyncSandboxStream,
    ImageCleanupResult,
    ImageClient,
    ImageInfo,
    ImageLibraryInfo,
    ImageLogLine,
    ImageTagInfo,
    Sandbox,
    SandboxClient,
    SandboxCreateResult,
    SandboxEnvValue,
    SandboxExpirationPolicyDate,
    SandboxExpirationPolicyIdle,
    SandboxExpirationPolicyMaxAge,
    SandboxFileSystem,
    SandboxFileSystemDirectory,
    SandboxFileSystemFileInfo,
    SandboxFileSystemFindMatch,
    SandboxFileSystemFindResult,
    SandboxFileSystemGrepMatch,
    SandboxFileSystemGrepResult,
    SandboxFileSystemSubdirectoryInfo,
    SandboxInfo,
    SandboxLifecycle,
    SandboxNetwork,
    SandboxNetworkProxy,
    SandboxNetworkProxyRoute,
    SandboxPort,
    SandboxProcess,
    SandboxProcessInfo,
    SandboxProcessLogs,
    SandboxStream,
)

# Methods of an async class that are plain functions, since they make no call.
_SYNC_IN_ASYNC = {(AsyncSandboxClient, "sandbox_from_url")}


@pytest.mark.parametrize(
    ("sync_cls", "async_cls"),
    [
        (SandboxClient, AsyncSandboxClient),
        (Sandbox, AsyncSandbox),
        (SandboxCreateResult, AsyncSandboxCreateResult),
        (SandboxProcess, AsyncSandboxProcess),
        (SandboxFileSystem, AsyncSandboxFileSystem),
        (ImageClient, AsyncImageClient),
        (SandboxStream, AsyncSandboxStream),
    ],
)
def test_sync_and_async_match(sync_cls: type, async_cls: type) -> None:
    sync_members = _public_members(sync_cls)
    async_members = _public_members(async_cls)
    assert sync_members.keys() == async_members.keys()
    for name, sync_member in sync_members.items():
        async_member = async_members[name]
        if isinstance(sync_member, property):
            assert isinstance(async_member, property), name
            continue
        assert _parameters(sync_member) == _parameters(async_member), name
        if inspect.isgeneratorfunction(sync_member):
            assert inspect.isasyncgenfunction(async_member), name
        elif (async_cls, name) in _SYNC_IN_ASYNC:
            assert not inspect.iscoroutinefunction(async_member), name
        else:
            assert inspect.iscoroutinefunction(async_member), name


@dataclasses.dataclass(kw_only=True)
class _Parity:
    """How one of our types or calls corresponds to a generated model."""

    ours: set[str]
    generated: type[BaseModel]
    renamed: dict[str, str] = dataclasses.field(default_factory=dict)
    """Generated field name to ours."""
    ours_only: dict[str, str] = dataclasses.field(default_factory=dict)
    """Our field to why the generated model lacks it."""
    generated_only: dict[str, str] = dataclasses.field(default_factory=dict)
    """Generated field to why we leave it out."""


def _dataclass_fields(cls: Any) -> set[str]:
    return {field.name for field in dataclasses.fields(cls)}


def _kwargs(method: Callable[..., Any]) -> set[str]:
    return {
        name
        for name, parameter in inspect.signature(method).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }


_PROCESS_RENAMES = {
    "workingDir": "working_dir",
    "keepAlive": "keep_alive",
    "maxRestarts": "max_restarts",
    "restartOnFailure": "restart_on_failure",
}
_PROCESS_REQUEST_RENAMES = {
    **_PROCESS_RENAMES,
    "waitForCompletion": "wait_for_completion",
    "waitForPorts": "wait_for_ports",
}

_POLICY_RENAMES = {"value": "after"}
_POLICY_TYPE = {"type": "set by which policy class is used"}

# A field the server adds fails here until it is mapped or listed.
_PARITY = {
    "SandboxInfo": _Parity(
        ours=_dataclass_fields(SandboxInfo),
        generated=baseten.client.managementapi.Sandbox,
    ),
    "SandboxEnvValue": _Parity(
        ours=_dataclass_fields(SandboxEnvValue),
        generated=baseten.client.managementapi.SandboxEnv,
        generated_only={"name": "the key envs are mapped by"},
    ),
    "SandboxLifecycle": _Parity(
        ours=_dataclass_fields(SandboxLifecycle),
        generated=baseten.client.managementapi.SandboxLifecycle,
    ),
    "SandboxExpirationPolicyIdle": _Parity(
        ours=_dataclass_fields(SandboxExpirationPolicyIdle),
        generated=baseten.client.managementapi.SandboxTTLIdleExpirationPolicy,
        renamed=_POLICY_RENAMES,
        generated_only=_POLICY_TYPE,
    ),
    "SandboxExpirationPolicyMaxAge": _Parity(
        ours=_dataclass_fields(SandboxExpirationPolicyMaxAge),
        generated=baseten.client.managementapi.SandboxTTLMaxAgeExpirationPolicy,
        renamed=_POLICY_RENAMES,
        generated_only=_POLICY_TYPE,
    ),
    "SandboxExpirationPolicyDate": _Parity(
        ours=_dataclass_fields(SandboxExpirationPolicyDate),
        generated=baseten.client.managementapi.SandboxDateExpirationPolicy,
        renamed={"value": "at"},
        generated_only=_POLICY_TYPE,
    ),
    "SandboxPort": _Parity(
        ours=_dataclass_fields(SandboxPort),
        generated=baseten.client.managementapi.SandboxPort,
    ),
    "SandboxNetwork": _Parity(
        ours=_dataclass_fields(SandboxNetwork),
        generated=baseten.client.managementapi.SandboxNetwork,
    ),
    "SandboxNetworkProxy": _Parity(
        ours=_dataclass_fields(SandboxNetworkProxy),
        generated=baseten.client.managementapi.SandboxProxyConfig,
    ),
    "SandboxNetworkProxyRoute": _Parity(
        ours=_dataclass_fields(SandboxNetworkProxyRoute),
        generated=baseten.client.managementapi.SandboxProxyTarget,
    ),
    "SandboxClient.create": _Parity(
        ours=_kwargs(SandboxClient.create),
        generated=baseten.client.managementapi.CreateSandboxRequest,
    ),
    "SandboxClient.update": _Parity(
        ours=_kwargs(SandboxClient.update) - {"name"},
        generated=baseten.client.managementapi.UpdateSandboxRequest,
    ),
    "SandboxProcessInfo": _Parity(
        ours=_dataclass_fields(SandboxProcessInfo),
        generated=baseten.client.sandboxapi.ProcessResponse,
        renamed={
            **_PROCESS_RENAMES,
            "exitCode": "exit_code",
            "startedAt": "started_at",
            "completedAt": "completed_at",
            "restartCount": "restart_count",
        },
    ),
    "SandboxProcessLogs": _Parity(
        ours=_dataclass_fields(SandboxProcessLogs),
        generated=baseten.client.sandboxapi.ProcessLogs,
    ),
    "SandboxProcess.exec": _Parity(
        ours=_kwargs(SandboxProcess.exec),
        generated=baseten.client.sandboxapi.ProcessRequest,
        renamed=_PROCESS_REQUEST_RENAMES,
    ),
    "SandboxProcess.exec_stream": _Parity(
        ours=_kwargs(SandboxProcess.exec_stream),
        generated=baseten.client.sandboxapi.ProcessRequest,
        renamed=_PROCESS_REQUEST_RENAMES,
        generated_only={"waitForCompletion": "always sent true, to stream"},
    ),
    "SandboxFileSystemDirectory": _Parity(
        ours=_dataclass_fields(SandboxFileSystemDirectory),
        generated=baseten.client.sandboxapi.Directory,
    ),
    "SandboxFileSystemFileInfo": _Parity(
        ours=_dataclass_fields(SandboxFileSystemFileInfo),
        generated=baseten.client.sandboxapi.File,
        renamed={"size": "size_bytes", "lastModified": "last_modified"},
    ),
    "SandboxFileSystemSubdirectoryInfo": _Parity(
        ours=_dataclass_fields(SandboxFileSystemSubdirectoryInfo),
        generated=baseten.client.sandboxapi.Subdirectory,
    ),
    "SandboxFileSystemFindResult": _Parity(
        ours=_dataclass_fields(SandboxFileSystemFindResult),
        generated=baseten.client.sandboxapi.FindResponse,
    ),
    "SandboxFileSystemFindMatch": _Parity(
        ours=_dataclass_fields(SandboxFileSystemFindMatch),
        generated=baseten.client.sandboxapi.FindMatch,
    ),
    "SandboxFileSystemGrepResult": _Parity(
        ours=_dataclass_fields(SandboxFileSystemGrepResult),
        generated=baseten.client.sandboxapi.ContentSearchResponse,
        generated_only={"query": "the caller's own query, echoed"},
    ),
    "SandboxFileSystemGrepMatch": _Parity(
        ours=_dataclass_fields(SandboxFileSystemGrepMatch),
        generated=baseten.client.sandboxapi.ContentSearchMatch,
    ),
    "ImageInfo": _Parity(
        ours=_dataclass_fields(ImageInfo),
        generated=baseten.client.managementapi.SandboxImage,
        renamed={"size": "size_bytes"},
        generated_only={"tags": "always empty; list_tags lists them"},
    ),
    "ImageInfo/Summary": _Parity(
        ours=_dataclass_fields(ImageInfo),
        generated=baseten.client.managementapi.SandboxImageSummary,
        renamed={"size": "size_bytes"},
    ),
    "ImageTagInfo": _Parity(
        ours=_dataclass_fields(ImageTagInfo),
        generated=baseten.client.managementapi.SandboxImageTag,
        renamed={"size": "size_bytes"},
    ),
    "ImageLogLine": _Parity(
        ours=_dataclass_fields(ImageLogLine),
        generated=baseten.client.managementapi.SandboxImageBuildLog,
        renamed={"message": "text"},
    ),
    "ImageCleanupResult": _Parity(
        ours=_dataclass_fields(ImageCleanupResult),
        generated=baseten.client.managementapi.CleanupSandboxImagesResponse,
    ),
    "ImageLibraryInfo": _Parity(
        ours=_dataclass_fields(ImageLibraryInfo),
        generated=baseten.client.managementapi.SandboxLibraryImage,
        ours_only={
            "creation_extra_args": "flattened from creation_options",
            "creation_volumes": "flattened from creation_options",
        },
        generated_only={
            "hidden": "always false in listings",
            "coming_soon": "always false in listings",
            "creation_options": "flattened into creation_* fields",
        },
    ),
    "ImageClient.list": _Parity(
        ours=_kwargs(ImageClient.list),
        generated=baseten.client.managementapi.ListImagesParams,
        renamed={"limit": "page_size", "q": "name_prefix"},
        generated_only={
            "team_id": "the client's team",
            "cursor": "paged internally",
        },
    ),
    "SandboxFileSystem.find": _Parity(
        ours=_kwargs(SandboxFileSystem.find) - {"path"},
        generated=baseten.client.sandboxapi.GetFilesystemFindParams,
        renamed={
            "maxResults": "max_results",
            "excludeDirs": "exclude_dirs",
            "excludeHidden": "exclude_hidden",
        },
    ),
    "SandboxFileSystem.grep": _Parity(
        ours=_kwargs(SandboxFileSystem.grep) - {"path"},
        generated=baseten.client.sandboxapi.GetFilesystemContentSearchParams,
        renamed={
            "caseSensitive": "case_sensitive",
            "maxResults": "max_results",
            "filePattern": "file_pattern",
            "excludeDirs": "exclude_dirs",
            "contextLines": "context_lines",
        },
    ),
}


@pytest.mark.parametrize("name", _PARITY)
def test_fields_match_generated(name: str) -> None:
    parity = _PARITY[name]
    generated = {
        parity.renamed.get(field, field)
        for field in parity.generated.model_fields
        if field not in parity.generated_only
    }
    assert generated == parity.ours - parity.ours_only.keys()


def _public_members(cls: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
    }


def _parameters(method: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    # Annotations differ between the two forms, so only names, kinds, and
    # defaults are compared.
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(method).parameters.values()
    ]

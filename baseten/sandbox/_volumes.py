from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

import baseten.client.managementapi
from baseten.sandbox._common import paginate, paginate_async
from baseten.sandbox._errors import converted_errors
from baseten.sandbox._info import set_fields

VolumeStatus: TypeAlias = Literal["DEPLOYING", "DEPLOYED", "FAILED", "DELETING"] | str
"""Status of a volume.

Other values may be added, so do not treat this list as exhaustive.
"""


@dataclass(kw_only=True)
class VolumeInfo:
    """A volume as last reported by the control plane."""

    name: str
    """Unique name of the volume."""

    size: str | int
    """Capacity, as a size such as ``"10Gi"`` or a number of megabytes."""

    region: str
    """Region of the volume. Only sandboxes in the same region can mount it."""

    status: VolumeStatus

    attached_to: str | None
    """Name of the sandbox the volume is mounted in, ``None`` when not mounted."""

    labels: dict[str, str] | None
    """Key-value pairs for organizing and filtering.

    ``None`` when not returned, since :meth:`VolumeClient.list` omits them.
    """

    created_at: datetime | None

    updated_at: datetime | None

    created_by: str | None
    """Who created the volume."""

    updated_by: str | None
    """Who last updated the volume."""


@dataclass(kw_only=True)
class VolumeClientContext:
    """What a :class:`VolumeClient` uses from the client that made it."""

    raw_api: baseten.client.managementapi.ApiClient
    team_id: str | None

    @contextmanager
    def api(self) -> Iterator[baseten.client.managementapi.ApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("control"):
            yield self.raw_api


@dataclass(kw_only=True)
class AsyncVolumeClientContext:
    """Async form of :class:`VolumeClientContext`."""

    raw_api: baseten.client.managementapi.AsyncApiClient
    team_id: str | None

    @contextmanager
    def api(self) -> Iterator[baseten.client.managementapi.AsyncApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("control"):
            yield self.raw_api


class VolumeClient:
    """Persistent volumes, which keep their data beyond the sandboxes they are mounted in.

    Mount one when creating a sandbox with its ``volumes`` option. Obtained
    from :attr:`SandboxClient.volumes`.
    """

    def __init__(self, context: VolumeClientContext) -> None:
        """Internal. Obtained from :attr:`SandboxClient.volumes`.

        :meta private:
        """
        self._context = context

    def create(
        self,
        *,
        size: str | int,
        name: str | None = None,
        region: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> VolumeInfo:
        """Create a volume.

        The volume becomes ready asynchronously, so its status may still be
        deploying.

        Args:
            size: Capacity, as a size such as ``"10Gi"`` or ``"512Mi"``, or a
                number of megabytes. It can grow later but never shrink.
            name: Unique name of the volume. The server assigns one when
                ``None``.
            region: Region of the volume. Defaults to the default sandbox
                region. Only sandboxes in the same region can mount it.
            labels: Key-value pairs for organizing and filtering.
        """
        with self._context.api() as api:
            volume = api.create_sandbox_volume(
                params=baseten.client.managementapi.CreateSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=_create_request(
                    size=size, name=name, region=region, labels=labels
                ),
            )
        return _volume_info(volume)

    def get_info(self, *, name: str) -> VolumeInfo:
        """Get a volume's current record.

        Args:
            name: Name of the volume.
        """
        with self._context.api() as api:
            volume = api.get_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.GetSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _volume_info(volume)

    def list(
        self,
        *,
        query: str | None = None,
        statuses: builtins.list[VolumeStatus] | None = None,
        page_size: int | None = None,
    ) -> Iterator[VolumeInfo]:
        """List volumes, newest first, fetching further pages as iteration reaches them.

        Records omit labels.

        Args:
            query: Searches volume names.
            statuses: Only volumes with one of these statuses.
            page_size: How many volumes to fetch per underlying request.
        """

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxVolumesResponse:
            with self._context.api() as api:
                return api.list_sandbox_volumes(
                    params=_list_params(
                        team_id=self._context.team_id,
                        cursor=cursor,
                        query=query,
                        statuses=statuses,
                        page_size=page_size,
                    )
                )

        for volume in paginate("volume list", fetch_page):
            yield _volume_info(volume)

    def update(
        self,
        *,
        name: str,
        size: str | int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> VolumeInfo:
        """Update a volume, returning its new record.

        Args:
            name: Name of the volume.
            size: New capacity, as a size such as ``"20Gi"`` or a number of
                megabytes, no less than the current capacity. ``None`` leaves
                it unchanged.
            labels: Labels replacing all current ones, including when empty.
                ``None`` leaves them unchanged.
        """
        with self._context.api() as api:
            volume = api.update_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.UpdateSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=_update_request(size=size, labels=labels),
            )
        return _volume_info(volume)

    def delete(self, *, name: str) -> VolumeInfo:
        """Delete a volume and its data. Fails while a sandbox mounts it.

        Deletion continues after this returns, so the record usually shows the
        volume as deleting.

        Args:
            name: Name of the volume.
        """
        with self._context.api() as api:
            volume = api.delete_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.DeleteSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _volume_info(volume)


class AsyncVolumeClient:
    """Async form of :class:`VolumeClient`."""

    def __init__(self, context: AsyncVolumeClientContext) -> None:
        """Internal. Obtained from :attr:`AsyncSandboxClient.volumes`.

        :meta private:
        """
        self._context = context

    async def create(
        self,
        *,
        size: str | int,
        name: str | None = None,
        region: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> VolumeInfo:
        """Create a volume. As :meth:`VolumeClient.create`."""
        with self._context.api() as api:
            volume = await api.create_sandbox_volume(
                params=baseten.client.managementapi.CreateSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=_create_request(
                    size=size, name=name, region=region, labels=labels
                ),
            )
        return _volume_info(volume)

    async def get_info(self, *, name: str) -> VolumeInfo:
        """Get a volume's current record. As :meth:`VolumeClient.get_info`."""
        with self._context.api() as api:
            volume = await api.get_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.GetSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _volume_info(volume)

    async def list(
        self,
        *,
        query: str | None = None,
        statuses: builtins.list[VolumeStatus] | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[VolumeInfo]:
        """List volumes. As :meth:`VolumeClient.list`."""

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxVolumesResponse:
            with self._context.api() as api:
                return await api.list_sandbox_volumes(
                    params=_list_params(
                        team_id=self._context.team_id,
                        cursor=cursor,
                        query=query,
                        statuses=statuses,
                        page_size=page_size,
                    )
                )

        async for volume in paginate_async("volume list", fetch_page):
            yield _volume_info(volume)

    async def update(
        self,
        *,
        name: str,
        size: str | int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> VolumeInfo:
        """Update a volume. As :meth:`VolumeClient.update`."""
        with self._context.api() as api:
            volume = await api.update_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.UpdateSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=_update_request(size=size, labels=labels),
            )
        return _volume_info(volume)

    async def delete(self, *, name: str) -> VolumeInfo:
        """Delete a volume. As :meth:`VolumeClient.delete`."""
        with self._context.api() as api:
            volume = await api.delete_sandbox_volume(
                volume_name=name,
                params=baseten.client.managementapi.DeleteSandboxVolumeParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _volume_info(volume)


def _create_request(
    *,
    size: str | int,
    name: str | None,
    region: str | None,
    labels: Mapping[str, str] | None,
) -> baseten.client.managementapi.CreateSandboxVolumeRequest:
    """Build a create request."""
    return baseten.client.managementapi.CreateSandboxVolumeRequest(
        **set_fields(
            size=size,
            name=name,
            region=region,
            labels=None if labels is None else dict(labels),
        )
    )


def _update_request(
    *, size: str | int | None, labels: Mapping[str, str] | None
) -> baseten.client.managementapi.UpdateSandboxVolumeRequest:
    """Build an update request."""
    return baseten.client.managementapi.UpdateSandboxVolumeRequest(
        **set_fields(size=size, labels=None if labels is None else dict(labels))
    )


def _list_params(
    *,
    team_id: str | None,
    cursor: str | None,
    query: str | None,
    statuses: list[VolumeStatus] | None,
    page_size: int | None,
) -> baseten.client.managementapi.ListSandboxVolumesParams:
    """Build a list page's query parameters."""
    return baseten.client.managementapi.ListSandboxVolumesParams(
        **set_fields(
            team_id=team_id,
            cursor=cursor,
            limit=page_size,
            q=query,
            status=statuses,
        )
    )


def _volume_info(volume: baseten.client.managementapi.SandboxVolume) -> VolumeInfo:
    """Convert a volume record."""
    return VolumeInfo(
        name=volume.name,
        size=volume.size,
        region=volume.region,
        status=volume.status,
        attached_to=volume.attached_to,
        labels=volume.labels,
        created_at=volume.created_at,
        updated_at=volume.updated_at,
        created_by=volume.created_by,
        updated_by=volume.updated_by,
    )

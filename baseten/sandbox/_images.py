from __future__ import annotations

import asyncio
import builtins
import contextlib
import inspect
import io
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO, Literal, TypeAlias, overload

import httpx

import baseten.client.managementapi
from baseten.sandbox._common import paginate, paginate_async
from baseten.sandbox._errors import (
    ImageBuildError,
    ImageUploadError,
    SandboxAPIError,
    SandboxError,
    converted_errors,
)
from baseten.sandbox._images_builder import ImageBuilder
from baseten.sandbox._images_ignore import (
    AsyncImageIgnoreFileFunc,
    AsyncImageIgnoreFileProcessor,
    ImageIgnoreFileFunc,
    ImageIgnoreFileProcessor,
    default_image_ignore_file,
    missing_processor_error,
    read_ignore_file,
)
from baseten.sandbox._info import SandboxPort, ports_from_api, set_fields
from baseten.sandbox._retry import is_transient_reset_error
from baseten.sandbox._zip import (
    ZipEntry,
    check_directory_dockerfile,
    check_zip_dockerfile,
    files_zip_entries,
    run_walk,
    run_walk_async,
    walk_directory,
    write_zip,
)

_DEFAULT_WAIT_TIMEOUT = timedelta(minutes=15)
_DEFAULT_POLL_INTERVAL = timedelta(seconds=3)

# The largest page and offset the build log endpoint accepts, which together
# cap a range at the 11,000 most recent lines.
_LOGS_PAGE_SIZE = 1000
_LOGS_MAX_OFFSET = 10_000

# Statuses a wait rides out, since the image may not be visible yet or the
# gateway may briefly fail.
_WAIT_RETRY_STATUSES = frozenset({404, 502, 503, 504})

# Size of each read of a zip in a temporary file while uploading it.
_UPLOAD_CHUNK_SIZE = 1024 * 1024

ImageStatus: TypeAlias = Literal["UPLOADING", "BUILDING", "BUILT", "FAILED"] | str
"""Processing status of an image. Only ``BUILT`` images are ready to use.

Other values may be added, so do not treat this list as exhaustive.
"""

ImageSort: TypeAlias = (
    Literal["name:asc", "name:desc", "createdAt:asc", "createdAt:desc"] | str
)
"""Sort order of an image listing.

Other values may be added, so do not treat this list as exhaustive.
"""

ImageTagSort: TypeAlias = Literal["name:asc", "name:desc"] | str
"""Sort order of a tag listing.

Other values may be added, so do not treat this list as exhaustive.
"""


@dataclass(kw_only=True)
class ImageInfo:
    """An image repository as last reported by the control plane."""

    name: str
    """Repository name given when pushing.

    To create a sandbox from the image, pass ``<name>:latest`` as its
    ``image``, or ``<name>:<tag>`` for a specific version.
    """

    status: ImageStatus

    created_at: datetime | None

    updated_at: datetime | None

    last_deployed_at: datetime | None
    """Most recent time any version of the image was used by a sandbox."""

    size_bytes: int | None
    """Total size of all versions."""

    tag_count: int | None
    """Number of versions, each with its own tag."""


@dataclass(kw_only=True)
class ImageTagInfo:
    """One version of an image."""

    name: str
    """Tag name, assigned by the service."""

    created_at: datetime | None

    updated_at: datetime | None

    size_bytes: int | None


@dataclass(kw_only=True)
class ImageCleanupResult:
    """Result of :meth:`ImageClient.cleanup`."""

    deleted: int
    """Number of image versions removed."""

    message: str
    """Human-readable description of the result."""


@dataclass(kw_only=True)
class ImageLogLine:
    """One line of an image's build log, from :meth:`ImageClient.logs`."""

    timestamp: datetime

    severity: int
    """Numeric OpenTelemetry severity level."""

    text: str


@dataclass(kw_only=True)
class ImageLibraryVolume:
    """A volume attachment a built-in image suggests, in :class:`ImageLibraryInfo`."""

    name: str
    """Volume name, or an internal identifier for an ephemeral volume."""

    mount_path: str
    """Absolute path the volume is mounted at."""

    type: str | None
    """Volume type, persistent when empty or ``None``."""

    size_mb: int | None
    """Storage capacity in megabytes of an ephemeral volume."""

    read_only: bool
    """Whether the volume is mounted read-only."""


@dataclass(kw_only=True)
class ImageLibraryInfo:
    """A built-in image available to every team.

    Usable as a sandbox's image without building or pushing it. From
    :meth:`ImageClient.list_library`.
    """

    name: str
    """Stable identifier of the image."""

    display_name: str | None
    """Human-readable name."""

    description: str | None
    """Short description."""

    long_description: str | None
    """Detailed description."""

    image: str
    """Image reference including its tag, to pass as a sandbox's ``image``."""

    memory: int | None
    """Recommended memory allocation in megabytes."""

    ports: list[SandboxPort]
    """Ports the image exposes."""

    categories: list[str]

    tags: list[str]

    url: str | None
    """Documentation URL."""

    icon: str | None
    """Icon URL."""

    icon_light: str | None
    """Light-mode icon URL."""

    icon_dark: str | None
    """Dark-mode icon URL."""

    enterprise: bool
    """Whether the image requires an enterprise plan."""

    creation_extra_args: dict[str, str] | None
    """Kernel selection arguments suggested when creating a sandbox from this image.

    Creating a sandbox cannot pass these yet.
    """

    creation_volumes: list[ImageLibraryVolume]
    """Volume attachments suggested when creating a sandbox from this image.

    Creating a sandbox cannot pass these yet.
    """


@dataclass(kw_only=True)
class ImageClientContext:
    """What an :class:`ImageClient` uses from the client that made it."""

    raw_api: baseten.client.managementapi.ApiClient
    team_id: str | None
    http_client: httpx.Client
    """Sends storage uploads, which take none of the API's authentication."""

    @contextmanager
    def api(self) -> Iterator[baseten.client.managementapi.ApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("control"):
            yield self.raw_api


@dataclass(kw_only=True)
class AsyncImageClientContext:
    """Async form of :class:`ImageClientContext`."""

    raw_api: baseten.client.managementapi.AsyncApiClient
    team_id: str | None
    http_client: httpx.AsyncClient
    """Sends storage uploads, which take none of the API's authentication."""

    @contextmanager
    def api(self) -> Iterator[baseten.client.managementapi.AsyncApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("control"):
            yield self.raw_api


@dataclass(kw_only=True)
class _Archive:
    """A zipped build context, in memory or in an anonymous temporary file."""

    file: IO[bytes]
    """Positioned at the start. Closing it removes a temporary file."""

    size: int


class ImageClient:
    """Images that sandboxes are created from.

    Pushes them from a build context or a registry, and finds and deletes
    them. Obtained from :attr:`SandboxClient.images`.
    """

    def __init__(self, context: ImageClientContext) -> None:
        """Internal. Obtained from :attr:`SandboxClient.images`.

        :meta private:
        """
        self._context = context

    @overload
    def push(
        self,
        *,
        name: str,
        builder: ImageBuilder,
        temp_file: bool | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    def push(
        self,
        *,
        name: str,
        registry_image: str,
        docker_config: str | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    def push(
        self,
        *,
        name: str,
        zip: bytes,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    def push(
        self,
        *,
        name: str,
        directory: str,
        ignore_file_processor: ImageIgnoreFileProcessor | None = ...,
        default_ignore_file: ImageIgnoreFileFunc | None = ...,
        temp_file: bool | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    def push(
        self,
        *,
        name: str,
        files: Mapping[str, str | bytes],
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    def push(
        self,
        *,
        name: str,
        builder: ImageBuilder | None = None,
        registry_image: str | None = None,
        docker_config: str | None = None,
        zip: bytes | None = None,
        directory: str | None = None,
        ignore_file_processor: ImageIgnoreFileProcessor | None = None,
        default_ignore_file: ImageIgnoreFileFunc | None = None,
        files: Mapping[str, str | bytes] | None = None,
        temp_file: bool | None = None,
        wait: bool = True,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_POLL_INTERVAL,
    ) -> ImageInfo:
        """Push an image, from a registry or from a build context built into one.

        Takes exactly one source. Waits for the image to be ready to use
        unless *wait* is false. To create a sandbox from it, pass
        ``<name>:latest`` as the sandbox's ``image``.

        A build context read from local files, from *directory* or a
        builder's local files and directories, is zipped into an anonymous
        temporary file unless *temp_file* is false; any other is zipped in
        memory. It is uploaded in one attempt. If the upload fails, the image
        is left as the service has it, so push again or delete it.

        Args:
            name: Repository name. Pushing to an existing name adds a new
                version to it.
            builder: Source: a builder whose Dockerfile and files make up the
                build context.
            registry_image: Source: a registry image reference to import,
                including its registry host.
            docker_config: Serialized registry credentials for a private
                *registry_image*.
            zip: Source: a zip archive of the build context, with a
                ``Dockerfile`` at its root.
            directory: Source: a local directory holding the build context,
                with a ``Dockerfile`` at its root. Files keep their
                permissions, a link to a file within the directory stores a
                copy of the file, a link to a file outside it fails the push, a
                link to a directory stores an empty directory, and a broken
                link is left out. Paths are left out as a ``.dockerignore`` at
                the directory's root says, read through
                *ignore_file_processor*, or else by *default_ignore_file*. The
                root ``Dockerfile`` and ``.dockerignore`` are always kept, as
                Docker keeps them.
            ignore_file_processor: Parses the ``.dockerignore`` at the root of
                *directory*. Required if there is one; otherwise the push
                fails before anything is sent. This package has no
                ``.dockerignore`` parser of its own.
            default_ignore_file: Filters *directory* when it has no
                ``.dockerignore``. Defaults to
                :func:`default_image_ignore_file`. Pass ``lambda _: False`` to
                push everything.
            files: Source: build context files by relative path, with forward
                slashes. Must include ``Dockerfile``. A string is written as
                UTF-8, and files get mode 0644.
            temp_file: Whether to zip a *directory* or a builder with local
                files into a temporary file, rather than in memory. Defaults to
                true.
            wait: Whether to return only once the image is ready to use.
            timeout: How long to wait for the image. ``None`` waits with no
                limit. Processing continues regardless.
            poll_interval: How long between checks while waiting.

        Raises:
            ImageUploadError: Uploading the build context failed.
            ImageBuildError: The build failed, or the wait timed out.
        """
        # Zipped before anything is sent, so a bad source leaves nothing behind.
        archive = self._source_archive(
            builder=builder,
            registry_image=registry_image,
            docker_config=docker_config,
            zip=zip,
            directory=directory,
            ignore_file_processor=ignore_file_processor,
            default_ignore_file=default_ignore_file,
            files=files,
            temp_file=temp_file,
        )
        try:
            with self._context.api() as api:
                response = api.push_image(
                    params=baseten.client.managementapi.PushImageParams(
                        **set_fields(team_id=self._context.team_id)
                    ),
                    request=_push_request(name, registry_image, docker_config),
                )
            if archive is not None:
                self._upload(name, _upload_url(name, response), archive)
        finally:
            if archive is not None:
                archive.file.close()
        if not wait:
            return _pushed_info(response)
        return self._wait_built(name, timeout, poll_interval)

    def wait_built(
        self,
        *,
        name: str,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_POLL_INTERVAL,
    ) -> ImageInfo:
        """Wait for an image to be ready to use.

        Checks the image's current status, so right after pushing a new
        version of an image that was already built, this may return before the
        new version is processed; push with *wait* left on to avoid that.

        Args:
            name: Name of the image.
            timeout: How long to wait. ``None`` waits with no limit.
                Processing continues regardless.
            poll_interval: How long between checks.

        Raises:
            ImageBuildError: The build failed, or the wait timed out.
        """
        return self._wait_built(name, timeout, poll_interval)

    def logs(
        self,
        *,
        name: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> builtins.list[ImageLogLine]:
        """Get the build log of an image's latest build, including a failed one.

        Oldest line first. Logs may take a short time to appear. Returns at
        most the 11,000 most recent lines in the range; narrow the range to see
        earlier ones.

        Args:
            name: Name of the image.
            start_time: Start of the range, inclusive. Defaults to 24 hours
                before *end_time*.
            end_time: End of the range. Defaults to now. The range must not
                exceed 7 days.
        """
        # Pinned on the first page, so the default range does not move while
        # paging through it.
        end = datetime.now(UTC) if end_time is None else end_time
        lines: builtins.list[ImageLogLine] = []
        for offset in range(0, _LOGS_MAX_OFFSET + 1, _LOGS_PAGE_SIZE):
            with self._context.api() as api:
                page = api.get_image_build_logs(
                    image_name=name,
                    params=_logs_params(self._context.team_id, start_time, end, offset),
                )
            if _add_log_page(lines, page, offset):
                break
        # Pages come newest first.
        lines.reverse()
        return lines

    def get_info(self, *, name: str) -> ImageInfo:
        """Get an image's current record.

        Args:
            name: Name of the image.
        """
        with self._context.api() as api:
            image = api.get_image(
                image_name=name,
                params=baseten.client.managementapi.GetImageParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    def list(
        self,
        *,
        name_prefix: str | None = None,
        sort: ImageSort | None = None,
        page_size: int | None = None,
    ) -> Iterator[ImageInfo]:
        """List images, fetching further pages as iteration reaches them.

        Args:
            name_prefix: Only images whose names start with this,
                case-sensitively.
            sort: Defaults to ``createdAt:desc``.
            page_size: How many images to fetch per underlying request.
        """

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxImagesResponse:
            with self._context.api() as api:
                return api.list_images(
                    params=baseten.client.managementapi.ListImagesParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                            sort=sort,
                            q=name_prefix,
                        )
                    )
                )

        for image in paginate("image list", fetch_page):
            yield _image_info(image)

    def delete(self, *, name: str) -> ImageInfo:
        """Delete an image and all its versions. Fails while a sandbox uses any version.

        Args:
            name: Name of the image.
        """
        with self._context.api() as api:
            image = api.delete_image(
                image_name=name,
                params=baseten.client.managementapi.DeleteImageParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    def list_tags(
        self,
        *,
        name: str,
        name_prefix: str | None = None,
        sort: ImageTagSort | None = None,
        page_size: int | None = None,
    ) -> Iterator[ImageTagInfo]:
        """List an image's versions, fetching further pages as iteration reaches them.

        Args:
            name: Name of the image.
            name_prefix: Only tags whose names start with this,
                case-sensitively. Sorts by name ascending.
            sort: Sort order of the tags.
            page_size: How many tags to fetch per underlying request.
        """

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxImageTagsResponse:
            with self._context.api() as api:
                return api.list_image_tags(
                    image_name=name,
                    params=baseten.client.managementapi.ListImageTagsParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                            sort=sort,
                            q=name_prefix,
                        )
                    ),
                )

        for tag in paginate("image tag list", fetch_page):
            yield _tag_info(tag)

    def delete_tag(self, *, name: str, tag: str) -> ImageInfo:
        """Delete one version of an image, returning the image as it is afterwards.

        Fails while a sandbox uses the version.

        Args:
            name: Name of the image.
            tag: Tag of the version to delete.
        """
        with self._context.api() as api:
            image = api.delete_image_tag(
                image_name=name,
                tag_name=tag,
                params=baseten.client.managementapi.DeleteImageTagParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    def cleanup(self) -> ImageCleanupResult:
        """Remove image versions that no sandbox uses."""
        with self._context.api() as api:
            result = api.cleanup_images(
                params=baseten.client.managementapi.CleanupImagesParams(
                    **set_fields(team_id=self._context.team_id)
                )
            )
        return ImageCleanupResult(deleted=result.deleted, message=result.message)

    def list_library(self) -> builtins.list[ImageLibraryInfo]:
        """List the built-in images available to every team."""
        with self._context.api() as api:
            result = api.list_sandbox_library_images()
        return [_library_info(image) for image in result.items]

    def _source_archive(
        self,
        *,
        builder: ImageBuilder | None,
        registry_image: str | None,
        docker_config: str | None,
        zip: bytes | None,
        directory: str | None,
        ignore_file_processor: ImageIgnoreFileProcessor | None,
        default_ignore_file: ImageIgnoreFileFunc | None,
        files: Mapping[str, str | bytes] | None,
        temp_file: bool | None,
    ) -> _Archive | None:
        """Check that a push has exactly one source, and zip it unless it is a registry image."""
        _check_push_sources(
            builder=builder,
            registry_image=registry_image,
            docker_config=docker_config,
            zip=zip,
            directory=directory,
            ignore_file_processor=ignore_file_processor,
            default_ignore_file=default_ignore_file,
            files=files,
            temp_file=temp_file,
        )
        if builder is not None:
            entries = builder._zip_entries()
            return _zip_archive(entries, _builder_to_file(entries, temp_file))
        if zip is not None:
            check_zip_dockerfile(zip)
            return _Archive(file=io.BytesIO(zip), size=len(zip))
        if directory is not None:
            check_directory_dockerfile(directory)
            ignore_file = read_ignore_file(directory)
            if ignore_file is None:
                ignore = default_ignore_file or default_image_ignore_file
            elif ignore_file_processor is None:
                raise missing_processor_error(ignore_file)
            else:
                ignore = ignore_file_processor(ignore_file)
            entries = run_walk(walk_directory(directory, ""), ignore)
            return _zip_archive(entries, temp_file is not False)
        if files is not None:
            return _zip_archive(files_zip_entries(files), False)
        return None

    def _upload(self, name: str, url: str, archive: _Archive) -> None:
        """Put a build context to the signed storage URL a push returned."""
        # The URL is signed for storage, so none of the API's headers or
        # credentials go with it. Storage rejects an upload without a length.
        http_client = self._context.http_client
        response = http_client.send(
            http_client.build_request(
                "PUT", url, content=archive.file, headers=_upload_headers(archive)
            ),
            auth=None,
        )
        if not response.is_success:
            raise ImageUploadError(
                image_name=name, status=response.status_code, body=response.text
            )

    # A push resets the image's status, so after one, BUILT or FAILED is that
    # push's outcome.
    def _wait_built(
        self, name: str, timeout: timedelta | None, poll_interval: timedelta
    ) -> ImageInfo:
        """Poll an image until it is built or failed."""
        # A check already in flight cannot be interrupted, so the wait can
        # run over by up to one check.
        deadline = (
            None if timeout is None else time.monotonic() + timeout.total_seconds()
        )
        last_status = "unknown"
        last_error: Exception | None = None
        while True:
            try:
                with self._context.api() as api:
                    image = api.get_image(
                        image_name=name,
                        params=baseten.client.managementapi.GetImageParams(
                            **set_fields(team_id=self._context.team_id)
                        ),
                    )
            except Exception as err:
                if not _is_retryable_wait_error(err):
                    raise
                last_error = err
            else:
                last_error = None
                last_status = image.status
                info = _built_image(name, image)
                if info is not None:
                    return info
            delay = poll_interval.total_seconds()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    time.sleep(max(remaining, 0))
                    raise ImageBuildError(
                        image_name=name, status=last_status, timed_out=True
                    ) from last_error
            time.sleep(delay)


class AsyncImageClient:
    """Async form of :class:`ImageClient`."""

    def __init__(self, context: AsyncImageClientContext) -> None:
        """Internal. Obtained from :attr:`AsyncSandboxClient.images`.

        :meta private:
        """
        self._context = context

    @overload
    async def push(
        self,
        *,
        name: str,
        builder: ImageBuilder,
        temp_file: bool | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    async def push(
        self,
        *,
        name: str,
        registry_image: str,
        docker_config: str | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    async def push(
        self,
        *,
        name: str,
        zip: bytes,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    async def push(
        self,
        *,
        name: str,
        directory: str,
        ignore_file_processor: AsyncImageIgnoreFileProcessor | None = ...,
        default_ignore_file: AsyncImageIgnoreFileFunc | None = ...,
        temp_file: bool | None = ...,
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    @overload
    async def push(
        self,
        *,
        name: str,
        files: Mapping[str, str | bytes],
        wait: bool = ...,
        timeout: timedelta | None = ...,
        poll_interval: timedelta = ...,
    ) -> ImageInfo: ...

    async def push(
        self,
        *,
        name: str,
        builder: ImageBuilder | None = None,
        registry_image: str | None = None,
        docker_config: str | None = None,
        zip: bytes | None = None,
        directory: str | None = None,
        ignore_file_processor: AsyncImageIgnoreFileProcessor | None = None,
        default_ignore_file: AsyncImageIgnoreFileFunc | None = None,
        files: Mapping[str, str | bytes] | None = None,
        temp_file: bool | None = None,
        wait: bool = True,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_POLL_INTERVAL,
    ) -> ImageInfo:
        """Push an image. As :meth:`ImageClient.push`.

        Local files are read and zipped on a worker thread. The ignore
        function and processor may also be coroutine functions.
        """
        archive = await self._source_archive(
            builder=builder,
            registry_image=registry_image,
            docker_config=docker_config,
            zip=zip,
            directory=directory,
            ignore_file_processor=ignore_file_processor,
            default_ignore_file=default_ignore_file,
            files=files,
            temp_file=temp_file,
        )
        try:
            with self._context.api() as api:
                response = await api.push_image(
                    params=baseten.client.managementapi.PushImageParams(
                        **set_fields(team_id=self._context.team_id)
                    ),
                    request=_push_request(name, registry_image, docker_config),
                )
            if archive is not None:
                await self._upload(name, _upload_url(name, response), archive)
        finally:
            if archive is not None:
                archive.file.close()
        if not wait:
            return _pushed_info(response)
        return await self._wait_built(name, timeout, poll_interval)

    async def wait_built(
        self,
        *,
        name: str,
        timeout: timedelta | None = _DEFAULT_WAIT_TIMEOUT,
        poll_interval: timedelta = _DEFAULT_POLL_INTERVAL,
    ) -> ImageInfo:
        """Wait for an image to be ready to use. As :meth:`ImageClient.wait_built`."""
        return await self._wait_built(name, timeout, poll_interval)

    async def logs(
        self,
        *,
        name: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> builtins.list[ImageLogLine]:
        """Get the build log of an image's latest build. As :meth:`ImageClient.logs`."""
        end = datetime.now(UTC) if end_time is None else end_time
        lines: builtins.list[ImageLogLine] = []
        for offset in range(0, _LOGS_MAX_OFFSET + 1, _LOGS_PAGE_SIZE):
            with self._context.api() as api:
                page = await api.get_image_build_logs(
                    image_name=name,
                    params=_logs_params(self._context.team_id, start_time, end, offset),
                )
            if _add_log_page(lines, page, offset):
                break
        lines.reverse()
        return lines

    async def get_info(self, *, name: str) -> ImageInfo:
        """Get an image's current record. As :meth:`ImageClient.get_info`."""
        with self._context.api() as api:
            image = await api.get_image(
                image_name=name,
                params=baseten.client.managementapi.GetImageParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    async def list(
        self,
        *,
        name_prefix: str | None = None,
        sort: ImageSort | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[ImageInfo]:
        """List images. As :meth:`ImageClient.list`."""

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxImagesResponse:
            with self._context.api() as api:
                return await api.list_images(
                    params=baseten.client.managementapi.ListImagesParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                            sort=sort,
                            q=name_prefix,
                        )
                    )
                )

        async for image in paginate_async("image list", fetch_page):
            yield _image_info(image)

    async def delete(self, *, name: str) -> ImageInfo:
        """Delete an image. As :meth:`ImageClient.delete`."""
        with self._context.api() as api:
            image = await api.delete_image(
                image_name=name,
                params=baseten.client.managementapi.DeleteImageParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    async def list_tags(
        self,
        *,
        name: str,
        name_prefix: str | None = None,
        sort: ImageTagSort | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[ImageTagInfo]:
        """List an image's versions. As :meth:`ImageClient.list_tags`."""

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxImageTagsResponse:
            with self._context.api() as api:
                return await api.list_image_tags(
                    image_name=name,
                    params=baseten.client.managementapi.ListImageTagsParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                            sort=sort,
                            q=name_prefix,
                        )
                    ),
                )

        async for tag in paginate_async("image tag list", fetch_page):
            yield _tag_info(tag)

    async def delete_tag(self, *, name: str, tag: str) -> ImageInfo:
        """Delete one version of an image. As :meth:`ImageClient.delete_tag`."""
        with self._context.api() as api:
            image = await api.delete_image_tag(
                image_name=name,
                tag_name=tag,
                params=baseten.client.managementapi.DeleteImageTagParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _image_info(image)

    async def cleanup(self) -> ImageCleanupResult:
        """Remove image versions that no sandbox uses. As :meth:`ImageClient.cleanup`."""
        with self._context.api() as api:
            result = await api.cleanup_images(
                params=baseten.client.managementapi.CleanupImagesParams(
                    **set_fields(team_id=self._context.team_id)
                )
            )
        return ImageCleanupResult(deleted=result.deleted, message=result.message)

    async def list_library(self) -> builtins.list[ImageLibraryInfo]:
        """List the built-in images. As :meth:`ImageClient.list_library`."""
        with self._context.api() as api:
            result = await api.list_sandbox_library_images()
        return [_library_info(image) for image in result.items]

    async def _source_archive(
        self,
        *,
        builder: ImageBuilder | None,
        registry_image: str | None,
        docker_config: str | None,
        zip: bytes | None,
        directory: str | None,
        ignore_file_processor: AsyncImageIgnoreFileProcessor | None,
        default_ignore_file: AsyncImageIgnoreFileFunc | None,
        files: Mapping[str, str | bytes] | None,
        temp_file: bool | None,
    ) -> _Archive | None:
        """Check that a push has exactly one source, and zip it unless it is a registry image."""
        _check_push_sources(
            builder=builder,
            registry_image=registry_image,
            docker_config=docker_config,
            zip=zip,
            directory=directory,
            ignore_file_processor=ignore_file_processor,
            default_ignore_file=default_ignore_file,
            files=files,
            temp_file=temp_file,
        )
        if builder is not None:
            entries = await asyncio.to_thread(builder._zip_entries)
            return await asyncio.to_thread(
                _zip_archive, entries, _builder_to_file(entries, temp_file)
            )
        if zip is not None:
            check_zip_dockerfile(zip)
            return _Archive(file=io.BytesIO(zip), size=len(zip))
        if directory is not None:
            await asyncio.to_thread(check_directory_dockerfile, directory)
            ignore_file = await asyncio.to_thread(read_ignore_file, directory)
            ignore: AsyncImageIgnoreFileFunc
            if ignore_file is None:
                ignore = default_ignore_file or default_image_ignore_file
            elif ignore_file_processor is None:
                raise missing_processor_error(ignore_file)
            else:
                processed = ignore_file_processor(ignore_file)
                ignore = (
                    await processed if inspect.isawaitable(processed) else processed
                )
            entries = await run_walk_async(walk_directory(directory, ""), ignore)
            return await asyncio.to_thread(
                _zip_archive, entries, temp_file is not False
            )
        if files is not None:
            return _zip_archive(files_zip_entries(files), False)
        return None

    async def _upload(self, name: str, url: str, archive: _Archive) -> None:
        """Put a build context to the signed storage URL a push returned."""

        # Read on a worker thread, since the archive may be a file on disk.
        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await asyncio.to_thread(
                archive.file.read, _UPLOAD_CHUNK_SIZE
            ):
                yield chunk

        http_client = self._context.http_client
        response = await http_client.send(
            http_client.build_request(
                "PUT", url, content=chunks(), headers=_upload_headers(archive)
            ),
            auth=None,
        )
        if not response.is_success:
            raise ImageUploadError(
                image_name=name, status=response.status_code, body=response.text
            )

    async def _wait_built(
        self, name: str, timeout: timedelta | None, poll_interval: timedelta
    ) -> ImageInfo:
        """Poll an image until it is built or failed."""
        last_status = "unknown"
        last_error: Exception | None = None
        scope = asyncio.timeout(None if timeout is None else timeout.total_seconds())
        try:
            async with scope:
                while True:
                    try:
                        with self._context.api() as api:
                            image = await api.get_image(
                                image_name=name,
                                params=baseten.client.managementapi.GetImageParams(
                                    **set_fields(team_id=self._context.team_id)
                                ),
                            )
                    except Exception as err:
                        if not _is_retryable_wait_error(err):
                            raise
                        last_error = err
                    else:
                        last_error = None
                        last_status = image.status
                        info = _built_image(name, image)
                        if info is not None:
                            return info
                    await asyncio.sleep(poll_interval.total_seconds())
        except TimeoutError:
            # Only this wait's own timeout, not a caller's or one from a
            # check, becomes the build error.
            if not scope.expired():
                raise
            raise ImageBuildError(
                image_name=name, status=last_status, timed_out=True
            ) from last_error


def _check_push_sources(
    *,
    builder: object,
    registry_image: object,
    docker_config: object,
    zip: object,
    directory: object,
    ignore_file_processor: object,
    default_ignore_file: object,
    files: object,
    temp_file: object,
) -> None:
    """Raise unless a push has exactly one source and only options that apply to it."""
    sources = [
        source
        for source, value in (
            ("builder", builder),
            ("registry_image", registry_image),
            ("zip", zip),
            ("directory", directory),
            ("files", files),
        )
        if value is not None
    ]
    if len(sources) != 1:
        raise TypeError(
            "an image push needs exactly one of builder, registry_image, zip, "
            f"directory, or files, got {', '.join(sources) or 'none'}"
        )
    if docker_config is not None and registry_image is None:
        raise TypeError("docker_config applies only to registry_image")
    if (
        ignore_file_processor is not None or default_ignore_file is not None
    ) and directory is None:
        raise TypeError(
            "ignore_file_processor and default_ignore_file apply only to directory"
        )
    if temp_file is not None and directory is None and builder is None:
        raise TypeError("temp_file applies only to directory and builder")


def _builder_to_file(entries: list[ZipEntry], temp_file: bool | None) -> bool:
    """Report whether a builder's build context is zipped into a temporary file."""
    # Only local files are read while zipping, so only they can make a build
    # context too large for memory.
    return temp_file is not False and any(
        entry.source_path is not None for entry in entries
    )


def _zip_archive(entries: list[ZipEntry], to_file: bool) -> _Archive:
    """Zip entries into an anonymous temporary file when ``to_file`` is set, or else in memory."""
    with contextlib.ExitStack() as on_failure:
        file: IO[bytes]
        if to_file:
            # A temporary file with no name, which the OS removes once it is
            # closed, even if the process dies first.
            file = on_failure.enter_context(tempfile.TemporaryFile())
        else:
            file = io.BytesIO()
        write_zip(entries, file)
        size = file.tell()
        file.seek(0)
        # Kept open for the upload, which closes it.
        on_failure.pop_all()
    return _Archive(file=file, size=size)


def _push_request(
    name: str, registry_image: str | None, docker_config: str | None
) -> baseten.client.managementapi.PushSandboxImageRequest:
    """Build the request that pushes an image."""
    return baseten.client.managementapi.PushSandboxImageRequest(
        name=name,
        **set_fields(image=registry_image, docker_config=docker_config),
    )


def _upload_url(
    name: str, response: baseten.client.managementapi.PushSandboxImageResponse
) -> str:
    """Give the storage URL a push returned for its build context."""
    if response.upload_url is None:
        raise SandboxError(f"pushing image {name} returned no upload URL")
    return str(response.upload_url)


def _upload_headers(archive: _Archive) -> dict[str, str]:
    """Give the headers of a build context upload."""
    return {"Content-Type": "application/zip", "Content-Length": str(archive.size)}


def _pushed_info(
    response: baseten.client.managementapi.PushSandboxImageResponse,
) -> ImageInfo:
    """Give the record a push returns without waiting."""
    return ImageInfo(
        name=response.name,
        status=response.status,
        created_at=None,
        updated_at=None,
        last_deployed_at=None,
        size_bytes=None,
        tag_count=None,
    )


def _built_image(
    name: str, image: baseten.client.managementapi.SandboxImage
) -> ImageInfo | None:
    """Give a built image's record, raise for a failed one, or ``None`` while it processes."""
    if image.status == "FAILED":
        raise ImageBuildError(image_name=name, status=image.status, timed_out=False)
    if image.status == "BUILT":
        return _image_info(image)
    # UPLOADING, BUILDING, or a status added later, all still processing.
    return None


def _is_retryable_wait_error(err: Exception) -> bool:
    """Report whether a wait rides out an error.

    A dropped connection, a gateway error, or a not-found while a push is
    still becoming visible.
    """
    if isinstance(err, SandboxAPIError):
        return err.status in _WAIT_RETRY_STATUSES
    return is_transient_reset_error(err)


def _logs_params(
    team_id: str | None, start_time: datetime | None, end_time: datetime, offset: int
) -> baseten.client.managementapi.GetImageBuildLogsParams:
    """Build the query of one page of build logs."""
    return baseten.client.managementapi.GetImageBuildLogsParams(
        end_time=end_time,
        limit=_LOGS_PAGE_SIZE,
        offset=offset,
        **set_fields(team_id=team_id, start_time=start_time),
    )


def _add_log_page(
    lines: list[ImageLogLine],
    page: baseten.client.managementapi.SandboxImageBuildLogsResponse,
    offset: int,
) -> bool:
    """Add a page of build logs to ``lines``, reporting whether it was the last."""
    lines.extend(
        ImageLogLine(timestamp=log.timestamp, severity=log.severity, text=log.message)
        for log in page.logs
    )
    return (
        len(page.logs) < _LOGS_PAGE_SIZE or offset + _LOGS_PAGE_SIZE >= page.total_count
    )


def _image_info(
    image: baseten.client.managementapi.SandboxImage
    | baseten.client.managementapi.SandboxImageSummary,
) -> ImageInfo:
    """Convert an image record, from a listing or not."""
    return ImageInfo(
        name=image.name,
        status=image.status,
        created_at=image.created_at,
        updated_at=image.updated_at,
        last_deployed_at=image.last_deployed_at,
        size_bytes=image.size,
        tag_count=image.tag_count,
    )


def _tag_info(tag: baseten.client.managementapi.SandboxImageTag) -> ImageTagInfo:
    """Convert an image version's record."""
    return ImageTagInfo(
        name=tag.name,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
        size_bytes=tag.size,
    )


# hidden and coming_soon are left out, since listings always have them false.
def _library_info(
    image: baseten.client.managementapi.SandboxLibraryImage,
) -> ImageLibraryInfo:
    """Convert a built-in image's record."""
    options = image.creation_options
    return ImageLibraryInfo(
        name=image.name,
        display_name=image.display_name,
        description=image.description,
        long_description=image.long_description,
        image=image.image,
        memory=image.memory,
        ports=ports_from_api(image.ports),
        categories=list(image.categories or []),
        tags=list(image.tags or []),
        url=image.url,
        icon=image.icon,
        icon_light=image.icon_light,
        icon_dark=image.icon_dark,
        enterprise=bool(image.enterprise),
        creation_extra_args=None
        if options is None or options.extra_args is None
        else dict(options.extra_args),
        creation_volumes=[]
        if options is None
        else [
            ImageLibraryVolume(
                name=volume.name,
                mount_path=volume.mount_path,
                type=volume.type,
                size_mb=volume.size_mb,
                read_only=bool(volume.read_only),
            )
            for volume in options.volumes or []
        ],
    )

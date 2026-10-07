from __future__ import annotations

import asyncio
import io
import json
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
import pytest

import baseten.sandbox._images
import baseten.sandbox._retry
from baseten.sandbox import (
    AsyncSandboxClient,
    ImageBuilder,
    ImageBuildError,
    ImageCleanupResult,
    ImageIgnoreFileOptions,
    ImageIgnoreFileProcessorOptions,
    ImageInfo,
    ImageLibraryInfo,
    ImageLibraryVolume,
    ImageLogLine,
    ImageTagInfo,
    ImageUploadError,
    SandboxAPIError,
    SandboxClient,
    SandboxError,
    SandboxPort,
)

_UPLOAD_URL = "https://storage.test/upload?signature=abc"

_NO_WAIT = timedelta(0)

_Reply = httpx.Response | Exception | dict[str, Any]


@pytest.fixture(autouse=True)
def short_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries take milliseconds instead of seconds.
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_base", 0.001)
    monkeypatch.setattr(baseten.sandbox._retry, "_backoff_max", 0.002)


class _Api:
    """Fake control plane and storage replying from per-route queues, recording requests.

    Each route replies in order and repeats its last reply.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[_Reply]] = {}

    def route(self, method: str, path: str, *replies: _Reply) -> None:
        self.routes[(method, path)] = list(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return httpx.Response(
                200,
                json={
                    "token": "token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                },
            )
        request.read()
        self.requests.append(request)
        replies = self.routes[(request.method, request.url.path)]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return httpx.Response(
                reply.status_code, headers=reply.headers, content=reply.content
            )
        return httpx.Response(200, json=reply)

    def client(self, **options: Any) -> SandboxClient:
        return SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(self.handle)
            ),
            **options,
        )

    def async_client(self) -> AsyncSandboxClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            await request.aread()
            return self.handle(request)

        return AsyncSandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ),
        )

    def paths(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.requests]

    def upload(self) -> httpx.Request:
        return next(r for r in self.requests if r.url.host == "storage.test")


def _image(status: str = "BUILT", **fields: Any) -> dict[str, Any]:
    return {
        "name": "img",
        "status": status,
        "created_at": "2026-10-07T12:00:00Z",
        "size": 1024,
        "tag_count": 2,
        "tags": [],
        **fields,
    }


def _pushing(api: _Api, *statuses: str) -> None:
    api.route(
        "POST",
        "/v1/sandboxes/images",
        httpx.Response(
            202, json={"name": "img", "status": "UPLOADING", "upload_url": _UPLOAD_URL}
        ),
    )
    api.route("PUT", "/upload", httpx.Response(200))
    api.route(
        "GET", "/v1/sandboxes/images/img", *(_image(status) for status in statuses)
    )


def _zip_names(request: httpx.Request) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(request.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _error(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": "NOPE", "message": "nope"})


class TestImageClientPush:
    def test_pushes_files_uploading_a_zip_without_auth_then_waits(self) -> None:
        api = _Api()
        _pushing(api, "BUILDING", "BUILT")
        info = api.client(team_id="team-1").images.push(
            name="img",
            files={"Dockerfile": "FROM scratch", "a.txt": b"a"},
            poll_interval=_NO_WAIT,
        )
        assert info.status == "BUILT"
        assert api.paths() == [
            "POST /v1/sandboxes/images",
            "PUT /upload",
            "GET /v1/sandboxes/images/img",
            "GET /v1/sandboxes/images/img",
        ]
        push = api.requests[0]
        assert push.url.params["team_id"] == "team-1"
        assert json.loads(push.content) == {"name": "img"}
        upload = api.upload()
        assert "Authorization" not in upload.headers
        assert upload.headers["Content-Type"] == "application/zip"
        assert upload.headers["Content-Length"] == str(len(upload.content))
        assert str(upload.url) == _UPLOAD_URL
        assert _zip_names(upload) == {"Dockerfile": b"FROM scratch", "a.txt": b"a"}

    def test_uploads_without_the_http_clients_own_headers_or_cookies(self) -> None:
        api = _Api()
        _pushing(api, "BUILT")
        client = SandboxClient(
            api_key="key",
            base_url_override="https://api.test",
            http_client_override=httpx.Client(
                transport=httpx.MockTransport(api.handle),
                headers={"Authorization": "Bearer other", "X-Other": "1"},
                cookies={"session": "secret"},
            ),
        )
        client.images.push(name="img", files={"Dockerfile": "x"})
        upload = api.upload()
        assert "Authorization" not in upload.headers
        assert "X-Other" not in upload.headers
        assert "Cookie" not in upload.headers

    def test_takes_built_at_the_first_poll(self) -> None:
        api = _Api()
        _pushing(api, "BUILT")
        info = api.client().images.push(name="img", files={"Dockerfile": "x"})
        assert info == ImageInfo(
            name="img",
            status="BUILT",
            created_at=datetime(2026, 10, 7, 12, tzinfo=UTC),
            updated_at=None,
            last_deployed_at=None,
            size_bytes=1024,
            tag_count=2,
        )

    def test_raises_image_build_error_when_the_build_fails(self) -> None:
        api = _Api()
        _pushing(api, "FAILED")
        with pytest.raises(ImageBuildError, match="failed to build") as raised:
            api.client().images.push(name="img", files={"Dockerfile": "x"})
        assert (raised.value.image_name, raised.value.status) == ("img", "FAILED")
        assert not raised.value.timed_out

    def test_raises_a_timed_out_image_build_error_when_the_wait_runs_out(self) -> None:
        api = _Api()
        _pushing(api, "BUILDING")
        with pytest.raises(ImageBuildError, match="still BUILDING") as raised:
            api.client().images.push(
                name="img", files={"Dockerfile": "x"}, timeout=_NO_WAIT
            )
        assert raised.value.timed_out

    def test_rides_out_404_and_gateway_errors_while_waiting(self) -> None:
        api = _Api()
        _pushing(api)
        api.route(
            "GET",
            "/v1/sandboxes/images/img",
            _error(404),
            _error(503),
            httpx.RemoteProtocolError("Server disconnected"),
            _image("BUILT"),
        )
        info = api.client().images.wait_built(name="img", poll_interval=_NO_WAIT)
        assert info.status == "BUILT"
        assert len(api.requests) == 4

    def test_stops_waiting_on_other_errors(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/images/img", _error(403))
        with pytest.raises(SandboxAPIError) as raised:
            api.client().images.wait_built(name="img")
        assert raised.value.status == 403

    def test_returns_the_push_response_without_waiting_when_wait_is_false(
        self,
    ) -> None:
        api = _Api()
        _pushing(api)
        info = api.client().images.push(
            name="img", files={"Dockerfile": "x"}, wait=False
        )
        assert (info.name, info.status) == ("img", "UPLOADING")
        assert api.paths() == ["POST /v1/sandboxes/images", "PUT /upload"]

    def test_imports_a_registry_image_without_uploading(self) -> None:
        api = _Api()
        api.route(
            "POST",
            "/v1/sandboxes/images",
            httpx.Response(
                202,
                json={"name": "img", "status": "BUILDING", "image": "registry/img:1"},
            ),
        )
        api.route("GET", "/v1/sandboxes/images/img", _image("BUILT"))
        api.client().images.push(
            name="img", registry_image="docker.io/library/python:3", docker_config="{}"
        )
        assert json.loads(api.requests[0].content) == {
            "name": "img",
            "image": "docker.io/library/python:3",
            "docker_config": "{}",
        }
        assert api.paths()[1:] == ["GET /v1/sandboxes/images/img"]

    def test_raises_image_upload_error_when_storage_rejects_without_waiting(
        self,
    ) -> None:
        api = _Api()
        _pushing(api)
        api.route("PUT", "/upload", httpx.Response(403, text="<Error>Denied</Error>"))
        with pytest.raises(ImageUploadError, match="push it again") as raised:
            api.client().images.push(name="img", files={"Dockerfile": "x"})
        assert (raised.value.status, raised.value.body) == (
            403,
            "<Error>Denied</Error>",
        )
        assert api.paths() == ["POST /v1/sandboxes/images", "PUT /upload"]

    def test_uploads_a_given_zip_as_is(self) -> None:
        api = _Api()
        _pushing(api, "BUILT")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("Dockerfile", "FROM x")
        api.client().images.push(name="img", zip=archive.getvalue())
        assert api.upload().content == archive.getvalue()

    @pytest.mark.parametrize(
        ("sources", "message"),
        [
            ({}, "got none"),
            (
                {"files": {"Dockerfile": "x"}, "registry_image": "r"},
                "got registry_image, files",
            ),
            (
                {"files": {"Dockerfile": "x"}, "docker_config": "{}"},
                "docker_config applies only",
            ),
            (
                {"files": {"Dockerfile": "x"}, "default_ignore_file": lambda _: False},
                "apply only to directory",
            ),
            ({"files": {"Dockerfile": "x"}, "temp_file": False}, "temp_file applies"),
            ({"files": {"a": "x"}}, "has no Dockerfile"),
            ({"zip": b"not a zip"}, "not a readable zip"),
        ],
    )
    def test_rejects_a_bad_source_before_sending_anything(
        self, sources: dict[str, Any], message: str
    ) -> None:
        api = _Api()
        with pytest.raises((TypeError, ValueError), match=message):
            api.client().images.push(name="img", **sources)
        assert api.requests == []

    def test_pushes_a_builder_as_a_zip_of_its_dockerfile_and_files(self) -> None:
        api = _Api()
        _pushing(api, "BUILT")
        builder = ImageBuilder.from_registry("debian:bookworm-slim").add_file(
            "/hello.txt", "hello"
        )
        api.client().images.push(name="img", builder=builder)
        assert _zip_names(api.upload()) == {
            "Dockerfile": builder.dockerfile().encode(),
            "hello.txt": b"hello",
        }


class TestImageClientPushDirectory:
    def _directory(self, root: Path) -> None:
        (root / "Dockerfile").write_text("FROM x")
        (root / "app.py").write_text("print()")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref")
        (root / "build").mkdir()
        (root / "build" / "out.bin").write_text("o")

    def test_leaves_out_the_defaults_without_a_dockerignore(
        self, tmp_path: Path
    ) -> None:
        self._directory(tmp_path)
        api = _Api()
        _pushing(api, "BUILT")
        api.client().images.push(name="img", directory=str(tmp_path))
        assert sorted(_zip_names(api.upload())) == [
            "Dockerfile",
            "app.py",
            "build/",
            "build/out.bin",
        ]

    def test_uses_default_ignore_file_instead_of_the_defaults(
        self, tmp_path: Path
    ) -> None:
        self._directory(tmp_path)
        api = _Api()
        _pushing(api, "BUILT")
        api.client().images.push(
            name="img",
            directory=str(tmp_path),
            default_ignore_file=lambda options: options.rel_path == "app.py",
        )
        assert "app.py" not in _zip_names(api.upload())
        assert ".git/HEAD" in _zip_names(api.upload())

    def test_reads_a_dockerignore_through_the_processor_pruning_directories(
        self, tmp_path: Path
    ) -> None:
        self._directory(tmp_path)
        (tmp_path / ".dockerignore").write_text("build\n")
        seen: list[ImageIgnoreFileProcessorOptions] = []

        def processor(
            options: ImageIgnoreFileProcessorOptions,
        ) -> Callable[[ImageIgnoreFileOptions], bool]:
            seen.append(options)
            return lambda candidate: candidate.rel_path in options.contents.split()

        api = _Api()
        _pushing(api, "BUILT")
        api.client().images.push(
            name="img", directory=str(tmp_path), ignore_file_processor=processor
        )
        assert seen == [
            ImageIgnoreFileProcessorOptions(
                path=str(tmp_path / ".dockerignore"), contents="build\n"
            )
        ]
        assert sorted(_zip_names(api.upload())) == [
            ".dockerignore",
            ".git/",
            ".git/HEAD",
            "Dockerfile",
            "app.py",
        ]

    def test_fails_before_sending_when_a_dockerignore_has_no_processor(
        self, tmp_path: Path
    ) -> None:
        self._directory(tmp_path)
        (tmp_path / ".dockerignore").write_text("build\n")
        api = _Api()
        with pytest.raises(ValueError, match="ignore_file_processor is not set"):
            api.client().images.push(name="img", directory=str(tmp_path))
        assert api.requests == []

    def test_rejects_a_directory_without_a_dockerfile_before_sending(
        self, tmp_path: Path
    ) -> None:
        api = _Api()
        with pytest.raises(ValueError, match="has no Dockerfile at its root"):
            api.client().images.push(name="img", directory=str(tmp_path))
        assert api.requests == []

    @pytest.mark.parametrize(("temp_file", "spooled"), [(None, True), (False, False)])
    def test_zips_a_directory_into_a_temporary_file_closed_after_upload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        temp_file: bool | None,
        spooled: bool,
    ) -> None:
        self._directory(tmp_path)
        files: list[IO[bytes]] = []
        original = tempfile.TemporaryFile

        def recording() -> IO[bytes]:
            file = original()
            files.append(file)
            return file

        monkeypatch.setattr(
            baseten.sandbox._images.tempfile, "TemporaryFile", recording
        )
        api = _Api()
        _pushing(api, "BUILT")
        api.client().images.push(
            name="img", directory=str(tmp_path), temp_file=temp_file
        )
        assert len(files) == (1 if spooled else 0)
        assert all(file.closed for file in files)
        upload = api.upload()
        assert upload.headers["Content-Length"] == str(len(upload.content))

    def test_closes_the_temporary_file_when_the_upload_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._directory(tmp_path)
        files: list[IO[bytes]] = []
        original = tempfile.TemporaryFile

        def recording() -> IO[bytes]:
            file = original()
            files.append(file)
            return file

        monkeypatch.setattr(
            baseten.sandbox._images.tempfile, "TemporaryFile", recording
        )
        api = _Api()
        _pushing(api)
        api.route("PUT", "/upload", httpx.Response(500))
        with pytest.raises(ImageUploadError):
            api.client().images.push(name="img", directory=str(tmp_path))
        assert len(files) == 1
        assert files[0].closed

    def test_spools_a_builder_only_when_it_has_local_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.txt").write_text("a")
        files: list[IO[bytes]] = []
        original = tempfile.TemporaryFile

        def recording() -> IO[bytes]:
            file = original()
            files.append(file)
            return file

        monkeypatch.setattr(
            baseten.sandbox._images.tempfile, "TemporaryFile", recording
        )
        api = _Api()
        _pushing(api, "BUILT")
        base = ImageBuilder.from_registry("debian:bookworm-slim")
        api.client().images.push(name="img", builder=base.add_file("/a", "a"))
        assert files == []
        api.client().images.push(
            name="img", builder=base.add_local_file(str(tmp_path / "a.txt"), "/a")
        )
        assert len(files) == 1


class TestImageClientListing:
    def test_lists_images_across_pages_with_filters_and_team(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images",
            {
                "items": [{"name": "a", "status": "BUILT"}],
                "pagination": {"has_more": True, "cursor": "c1"},
            },
            {
                "items": [{"name": "b", "status": "FAILED"}],
                "pagination": {"has_more": False},
            },
        )
        images = list(
            api.client(team_id="t").images.list(
                name_prefix="a", sort="name:asc", page_size=1
            )
        )
        assert [(image.name, image.status) for image in images] == [
            ("a", "BUILT"),
            ("b", "FAILED"),
        ]
        assert dict(api.requests[0].url.params) == {
            "team_id": "t",
            "limit": "1",
            "sort": "name:asc",
            "q": "a",
        }
        assert api.requests[1].url.params["cursor"] == "c1"

    def test_stops_listing_on_a_repeated_cursor(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images",
            {"items": [], "pagination": {"has_more": True, "cursor": "same"}},
        )
        with pytest.raises(SandboxError, match="image list returned a repeated cursor"):
            list(api.client().images.list())

    def test_lists_tags_deletes_a_tag_gets_deletes_and_cleans_up(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images/img/tags",
            {
                "items": [{"name": "abc123", "size": 10}],
                "pagination": {"has_more": False},
            },
        )
        api.route("DELETE", "/v1/sandboxes/images/img/tags/abc123", _image())
        api.route("GET", "/v1/sandboxes/images/img", _image())
        api.route("DELETE", "/v1/sandboxes/images/img", _image("BUILT", tag_count=0))
        api.route(
            "POST", "/v1/sandboxes/cleanup_images", {"deleted": 3, "message": "done"}
        )
        images = api.client().images
        assert list(images.list_tags(name="img", name_prefix="abc")) == [
            ImageTagInfo(name="abc123", created_at=None, updated_at=None, size_bytes=10)
        ]
        assert api.requests[0].url.params["q"] == "abc"
        assert images.delete_tag(name="img", tag="abc123").tag_count == 2
        assert images.get_info(name="img").size_bytes == 1024
        assert images.delete(name="img").tag_count == 0
        assert images.cleanup() == ImageCleanupResult(deleted=3, message="done")

    def test_lists_library_images(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/library_images",
            {
                "items": [
                    {
                        "name": "base-image",
                        "image": "baseten/base-image:latest",
                        "ports": [{"target": 8080, "protocol": "HTTP"}],
                        "hidden": False,
                        "creation_options": {
                            "extra_args": {"kernel": "x"},
                            "volumes": [{"name": "v", "mount_path": "/data"}],
                        },
                    }
                ]
            },
        )
        assert api.client().images.list_library() == [
            ImageLibraryInfo(
                name="base-image",
                display_name=None,
                description=None,
                long_description=None,
                image="baseten/base-image:latest",
                memory=None,
                ports=[SandboxPort(target=8080, protocol="HTTP")],
                categories=[],
                tags=[],
                url=None,
                icon=None,
                icon_light=None,
                icon_dark=None,
                enterprise=False,
                creation_extra_args={"kernel": "x"},
                creation_volumes=[
                    ImageLibraryVolume(
                        name="v",
                        mount_path="/data",
                        type=None,
                        size_mb=None,
                        read_only=False,
                    )
                ],
            )
        ]
        assert "team_id" not in api.requests[0].url.params


class TestImageClientLogs:
    def _log(self, second: int) -> dict[str, Any]:
        return {
            "timestamp": f"2026-10-07T12:00:{second:02d}Z",
            "message": f"line {second}",
            "severity": 9,
        }

    def test_gets_every_page_oldest_first_with_the_end_time_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(baseten.sandbox._images, "_LOGS_PAGE_SIZE", 2)
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images/img/logs",
            {"logs": [self._log(3), self._log(2)], "total_count": 3},
            {"logs": [self._log(1)], "total_count": 3},
        )
        lines = api.client().images.logs(name="img")
        assert lines == [
            ImageLogLine(
                timestamp=datetime(2026, 10, 7, 12, 0, second, tzinfo=UTC),
                severity=9,
                text=f"line {second}",
            )
            for second in (1, 2, 3)
        ]
        params = [request.url.params for request in api.requests]
        assert [p["offset"] for p in params] == ["0", "2"]
        assert params[0]["end_time"] == params[1]["end_time"]
        assert "start_time" not in params[0]

    def test_sends_the_given_range_and_stops_at_a_short_page(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images/img/logs",
            {"logs": [self._log(1)], "total_count": 5000},
        )
        start = datetime(2026, 10, 7, 11, tzinfo=UTC)
        end = datetime(2026, 10, 7, 12, tzinfo=UTC)
        api.client().images.logs(name="img", start_time=start, end_time=end)
        assert len(api.requests) == 1
        params = api.requests[0].url.params
        assert datetime.fromisoformat(params["start_time"]) == start
        assert datetime.fromisoformat(params["end_time"]) == end
        assert params["limit"] == "1000"

    def test_stops_at_the_11000_most_recent_lines(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images/img/logs",
            {"logs": [self._log(1)] * 1000, "total_count": 50_000},
        )
        lines = api.client().images.logs(name="img")
        assert len(lines) == 11_000
        assert api.requests[-1].url.params["offset"] == "10000"


class TestAsyncImageClient:
    @pytest.mark.asyncio
    async def test_pushes_a_directory_with_an_async_ignore_then_waits(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Dockerfile").write_text("FROM x")
        (tmp_path / "keep.txt").write_text("k")
        (tmp_path / "skip.txt").write_text("s")
        (tmp_path / ".dockerignore").write_text("skip.txt")

        async def processor(
            options: ImageIgnoreFileProcessorOptions,
        ) -> Callable[[ImageIgnoreFileOptions], Any]:
            async def ignore(candidate: ImageIgnoreFileOptions) -> bool:
                return candidate.rel_path == options.contents

            return ignore

        api = _Api()
        _pushing(api, "BUILDING", "BUILT")
        info = await api.async_client().images.push(
            name="img",
            directory=str(tmp_path),
            ignore_file_processor=processor,
            poll_interval=_NO_WAIT,
        )
        assert info.status == "BUILT"
        upload = api.upload()
        assert sorted(_zip_names(upload)) == [".dockerignore", "Dockerfile", "keep.txt"]
        assert upload.headers["Content-Length"] == str(len(upload.content))
        assert "Authorization" not in upload.headers

    @pytest.mark.asyncio
    async def test_times_out_waiting_as_an_image_build_error(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/images/img", _image("BUILDING"))
        with pytest.raises(ImageBuildError) as raised:
            await api.async_client().images.wait_built(name="img", timeout=_NO_WAIT)
        assert raised.value.timed_out

    @pytest.mark.asyncio
    async def test_leaves_the_callers_own_timeout_alone(self) -> None:
        api = _Api()
        api.route("GET", "/v1/sandboxes/images/img", _image("BUILDING"))
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await api.async_client().images.wait_built(
                    name="img", timeout=None, poll_interval=timedelta(seconds=10)
                )

    @pytest.mark.asyncio
    async def test_lists_and_gets_logs(self) -> None:
        api = _Api()
        api.route(
            "GET",
            "/v1/sandboxes/images",
            {
                "items": [{"name": "a", "status": "BUILT"}],
                "pagination": {"has_more": False},
            },
        )
        api.route(
            "GET",
            "/v1/sandboxes/images/a/logs",
            {
                "logs": [
                    {"timestamp": "2026-10-07T12:00:00Z", "message": "m", "severity": 9}
                ],
                "total_count": 1,
            },
        )
        images = api.async_client().images
        assert [image.name async for image in images.list()] == ["a"]
        assert [line.text for line in await images.logs(name="a")] == ["m"]

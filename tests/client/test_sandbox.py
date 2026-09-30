from __future__ import annotations

import httpx
import pytest

import baseten.client.sandboxapi
from baseten.client._sandbox import AsyncSandboxClient, SandboxClient
from tests.conftest import FakeTransport

SANDBOX_URL = "https://sb-123.b10.co"


def make_sync_client(fake: FakeTransport) -> SandboxClient:
    client = SandboxClient(token="test-token", base_url=SANDBOX_URL)
    client.http_client._transport = fake.sync_transport  # type: ignore[attr-defined]
    return client


def make_async_client(fake: FakeTransport) -> AsyncSandboxClient:
    client = AsyncSandboxClient(token="test-token", base_url=SANDBOX_URL)
    client.http_client._transport = fake.async_transport  # type: ignore[attr-defined]
    return client


def test_bearer_token_sent() -> None:
    fake = FakeTransport(200, {"message": "deleted"})
    client = make_sync_client(fake)

    client.api.delete_process(identifier="proc-1")

    assert fake.capture.path == "/process/proc-1"
    assert fake.capture.headers["authorization"] == "Bearer test-token"
    client.close()


def test_empty_token_omits_authorization() -> None:
    fake = FakeTransport(200, {"message": "deleted"})
    client = SandboxClient(token="", base_url=SANDBOX_URL)
    client.http_client._transport = fake.sync_transport  # type: ignore[attr-defined]

    client.api.delete_process(identifier="proc-1")

    assert "authorization" not in fake.capture.headers
    client.close()


def test_multipart_files_upload() -> None:
    fake = FakeTransport(200, {"etag": "abc", "partNumber": 3, "size": 9})
    client = make_sync_client(fake)

    resp = client.api.put_filesystem_multipart_part(
        upload_id="upload-1",
        params=baseten.client.sandboxapi.PutFilesystemMultipartPartParams(partNumber=3),
        files={"file": ("part.bin", b"part data")},
    )

    assert resp.etag == "abc"
    path, _, query = fake.capture.path.partition("?")
    assert path == "/filesystem-multipart/upload-1/part"
    assert query == "partNumber=3"
    # httpx derives the multipart Content-Type, boundary included.
    assert fake.capture.headers["content-type"].startswith("multipart/form-data")
    assert b"part data" in fake.capture.body_bytes
    client.close()


def test_octet_stream_body_sent_unmodified() -> None:
    fake = FakeTransport(200, {"message": "written"})
    client = make_sync_client(fake)

    client.api.post_process_stdin(identifier="proc-1", content=b"raw stdin")

    assert fake.capture.path == "/process/proc-1/stdin"
    assert fake.capture.headers["content-type"] == "application/octet-stream"
    assert fake.capture.body_bytes == b"raw stdin"
    client.close()


def test_raw_returns_response_unread_with_accept() -> None:
    fake = FakeTransport(
        200, raw_content=b"file bytes", content_type="application/octet-stream"
    )
    client = make_sync_client(fake)

    response = client.api.get_filesystem_raw(
        path="file.txt", accept="application/octet-stream"
    )

    assert isinstance(response, httpx.Response)
    assert response.content == b"file bytes"
    assert fake.capture.headers["accept"] == "application/octet-stream"
    client.close()


def test_raw_only_stream_keeps_plain_name() -> None:
    fake = FakeTransport(200, raw_content=b"log line\n", content_type="text/plain")
    client = make_sync_client(fake)

    response = client.api.get_process_logs_stream(identifier="proc-1")

    assert response.content == b"log line\n"
    assert fake.capture.headers["accept"] == "text/plain"
    client.close()


def test_export_narrows_on_status_code() -> None:
    fake = FakeTransport(
        200, {"manifest": {"createdAt": "now", "root": "/", "version": 1}, "size": 1024}
    )
    client = make_sync_client(fake)

    resp = client.api.post_archive_export(
        request=baseten.client.sandboxapi.ExportOptions()
    )

    assert isinstance(resp, baseten.client.sandboxapi.ExportResult)
    client.close()

    fake = FakeTransport(202, {})
    client = make_sync_client(fake)

    resp = client.api.post_archive_export(
        request=baseten.client.sandboxapi.ExportOptions()
    )

    assert isinstance(resp, baseten.client.sandboxapi.ExportProgress)
    client.close()


def test_query_params_serialize() -> None:
    fake = FakeTransport(200, {"message": "watching"})
    client = make_sync_client(fake)

    client.api.get_watch_filesystem(
        path="dir",
        params=baseten.client.sandboxapi.GetWatchFilesystemParams(ignore="*.log"),
    )

    path, _, query = fake.capture.path.partition("?")
    assert path == "/watch/filesystem/dir"
    assert query == "ignore=%2A.log"
    client.close()


def test_typed_error_keeps_status() -> None:
    fake = FakeTransport(400, {"error": "boom"})
    client = make_sync_client(fake)

    with pytest.raises(baseten.client.sandboxapi.ResponseErrorResponse) as exc_info:
        client.api.post_process(
            request=baseten.client.sandboxapi.ProcessRequest(command="ls")
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_response.error == "boom"
    client.close()


def test_untyped_error_on_non_schema_body() -> None:
    fake = FakeTransport(
        400, raw_content=b"<html>proxy error</html>", content_type="text/html"
    )
    client = make_sync_client(fake)

    with pytest.raises(baseten.client.sandboxapi.ResponseError) as exc_info:
        client.api.post_process(
            request=baseten.client.sandboxapi.ProcessRequest(command="ls")
        )

    assert exc_info.value.status_code == 400
    client.close()


@pytest.mark.asyncio
async def test_async_client_round_trip() -> None:
    fake = FakeTransport(200, {"message": "deleted"})
    client = make_async_client(fake)

    resp = await client.api.delete_process(identifier="proc-1")

    assert resp.message == "deleted"
    assert fake.capture.headers["authorization"] == "Bearer test-token"
    await client.close()


@pytest.mark.asyncio
async def test_async_raw_sibling() -> None:
    fake = FakeTransport(
        200, raw_content=b"event: done", content_type="text/event-stream"
    )
    client = make_async_client(fake)

    response = await client.api.post_process_raw(
        request=baseten.client.sandboxapi.ProcessRequest(command="ls"),
        accept="text/event-stream",
    )

    assert response.content == b"event: done"
    assert fake.capture.headers["accept"] == "text/event-stream"
    await client.close()

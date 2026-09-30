"""End-to-end tests against a live environment.

Skipped unless BASETEN_E2E_TEST_API_KEY and BASETEN_E2E_TEST_DOMAIN are set.
The key must belong to an organization with sandboxes enabled.
BASETEN_E2E_TEST_SANDBOXES_DOMAIN optionally points the /v1/sandboxes calls
at a different host than the token exchange, which stays on the management
domain. Mirrors the JS suite's environment contract.
"""

from __future__ import annotations

import os
import time

import pytest

import baseten.client.sandboxapi
from baseten.sandbox import AsyncSandboxClient, SandboxClient

API_KEY = os.environ.get("BASETEN_E2E_TEST_API_KEY", "")
MANAGEMENT_DOMAIN = os.environ.get("BASETEN_E2E_TEST_DOMAIN", "")
SANDBOXES_DOMAIN = (
    os.environ.get("BASETEN_E2E_TEST_SANDBOXES_DOMAIN") or MANAGEMENT_DOMAIN
)

e2e_required = pytest.mark.skipif(
    not (API_KEY and MANAGEMENT_DOMAIN), reason="e2e environment not set"
)

# The one region where the execution plane accepts staging-minted tokens
# (eu-dub-1 rejects them with AUTHENTICATION_FAILED).
E2E_REGION = "us-was-1"


def unique_sandbox_name() -> str:
    return f"baseten-python-e2e-{time.time():.0f}"


def make_client() -> SandboxClient:
    return SandboxClient(
        api_key=API_KEY,
        base_url_override=f"https://{MANAGEMENT_DOMAIN}",
        sandboxes_base_url_override=(
            f"https://{SANDBOXES_DOMAIN}"
            if SANDBOXES_DOMAIN != MANAGEMENT_DOMAIN
            else None
        ),
    )


@e2e_required
def test_create_exec_fs_delete() -> None:
    name = unique_sandbox_name()
    client = make_client()
    try:
        sandbox = client.create(name=name, region=E2E_REGION)
        assert sandbox.info is not None
        assert sandbox.info.status == "DEPLOYED"
        assert sandbox.info.url is not None

        process = sandbox.process.exec("echo hello", wait_for_completion=True)
        assert process.status == "completed"
        assert process.exit_code == 0
        assert process.stdout == "hello\n"

        sandbox.fs.write("/tmp/e2e.txt", "python sdk")
        assert sandbox.fs.read("/tmp/e2e.txt") == "python sdk"
    finally:
        deleted = client.delete(name)
        assert deleted.status in ("DELETING", "TERMINATED")
        client.close()


@e2e_required
@pytest.mark.asyncio
async def test_async_create_and_list() -> None:
    name = unique_sandbox_name()
    client = AsyncSandboxClient(
        api_key=API_KEY,
        base_url_override=f"https://{MANAGEMENT_DOMAIN}",
        sandboxes_base_url_override=(
            f"https://{SANDBOXES_DOMAIN}"
            if SANDBOXES_DOMAIN != MANAGEMENT_DOMAIN
            else None
        ),
    )
    try:
        sandbox = await client.create(name=name, region=E2E_REGION)
        assert sandbox.name == name

        listed = [info.name async for info in client.list(query=name)]
        assert name in listed
    finally:
        await client.delete(name)
        await client.close()


@e2e_required
def test_get_info_and_get() -> None:
    name = unique_sandbox_name()
    client = make_client()
    try:
        created = client.create(name=name, region=E2E_REGION)
        info = client.get_info(name)
        assert info.name == name
        assert info.status == "DEPLOYED"
        # The record keeps the server's trailing slash; Sandbox.url normalizes it.
        assert info.url is not None
        assert info.url.rstrip("/") == created.url

        sandbox = client.get(name)
        assert sandbox.name == name
        assert sandbox.url == created.url
    finally:
        client.delete(name)
        client.close()


@e2e_required
def test_list_paginates_across_pages() -> None:
    prefix = f"baseten-python-e2e-{time.time():.0f}"
    names = [f"{prefix}-a", f"{prefix}-b"]
    client = make_client()
    try:
        for sandbox_name in names:
            client.create(name=sandbox_name, region=E2E_REGION)

        listed = [info.name for info in client.list(query=prefix, page_size=1)]
        assert sorted(listed) == sorted(names)
    finally:
        for sandbox_name in names:
            client.delete(sandbox_name)
        client.close()


@e2e_required
@pytest.mark.xfail(
    reason="server bug: POST /process with waitForCompletion=false sends"
    " logs: null, but the spec declares logs required; pydantic rejects the"
    " response (JS does not validate, so only Python fails). Tracked with Chad.",
    strict=True,
)
def test_logs_stream_and_stdin_via_raw_api() -> None:
    name = unique_sandbox_name()
    client = make_client()
    try:
        sandbox = client.create(name=name, region=E2E_REGION)
        # `cat` with a stdin pipe, driven through the raw execution client:
        # the surface's exec does not expose stdin yet.
        sandbox.api.post_process(
            request=baseten.client.sandboxapi.ProcessRequest(
                command="cat", stdin=True, name="piper", waitForCompletion=False
            )
        )
        sandbox.api.post_process_stdin(
            identifier="piper", content=b"stdin round trip\n"
        )
        # Closing stdin makes cat exit; closing again is the no-op probe.
        sandbox.api.delete_process_stdin(identifier="piper")
        for _ in range(20):
            time.sleep(0.5)
            process = sandbox.api.get_process_identifier(identifier="piper")
            if process.status == "completed":
                assert "stdin round trip" in process.stdout
                break
        else:
            raise AssertionError("cat did not complete after stdin closed")

        logs_response = sandbox.api.get_process_logs_stream(identifier="piper")
        logs_response.read()
        logs_response.close()
        assert "stdin round trip" in logs_response.text
    finally:
        client.delete(name)
        client.close()


@e2e_required
def test_multipart_upload_via_raw_api() -> None:
    name = unique_sandbox_name()
    client = make_client()
    try:
        sandbox = client.create(name=name, region=E2E_REGION)
        remote_path = "/tmp/multipart-e2e.txt"
        initiated = sandbox.api.post_filesystem_multipart_initiate(
            path=remote_path,
            request=baseten.client.sandboxapi.MultipartInitiateRequest(),
        )
        upload_id = initiated.uploadId
        assert upload_id is not None

        part = sandbox.api.put_filesystem_multipart_part(
            upload_id=upload_id,
            params=baseten.client.sandboxapi.PutFilesystemMultipartPartParams(
                partNumber=1
            ),
            files={"file": ("part.bin", b"multipart e2e content")},
        )
        assert part.partNumber == 1

        completed = sandbox.api.post_filesystem_multipart_complete(
            upload_id=upload_id,
            request=baseten.client.sandboxapi.MultipartCompleteRequest(
                parts=[
                    baseten.client.sandboxapi.MultipartPartInfo(
                        partNumber=part.partNumber, etag=part.etag
                    )
                ]
            ),
        )
        assert completed.message

        assert sandbox.fs.read(remote_path) == "multipart e2e content"
    finally:
        client.delete(name)
        client.close()


@e2e_required
def test_archive_export_status_dispatch() -> None:
    name = unique_sandbox_name()
    client = make_client()
    try:
        sandbox = client.create(name=name, region=E2E_REGION)
        sandbox.fs.write("/tmp/archive-e2e.txt", "archive me")

        result = sandbox.api.post_archive_export(
            request=baseten.client.sandboxapi.ExportOptions(dryRun=True)
        )
        assert isinstance(result, baseten.client.sandboxapi.ExportResult)
        assert result.manifest is not None

        # The async (202 ExportProgress) path needs a presigned upload URL,
        # which the test cannot mint; it stays covered by unit tests.
    finally:
        client.delete(name)
        client.close()

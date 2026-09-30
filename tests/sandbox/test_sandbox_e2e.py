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

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from tests.sandbox.test_fs import _body, _Fake, _ok, _process


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("source", "destination"),
    [("-source", "copy"), ("source", "-destination"), ("-source", "-destination")],
)
@pytest.mark.asyncio
async def test_copy_preserves_paths_starting_with_dash(
    tmp_path: Path, source: str, destination: str, asynchronous: bool
) -> None:
    (tmp_path / source).write_text("file contents")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # Execute the SDK's actual command against local fixture files.
            result = subprocess.run(
                _body(request)["command"],
                shell=True,
                check=False,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return _ok(
                _process(
                    status="completed" if result.returncode == 0 else "failed",
                    exitCode=result.returncode,
                    stderr=result.stderr,
                )
            )
        return _ok(_process())

    fake = _Fake(handle)
    if asynchronous:
        await fake.async_sandbox().fs.copy(source=source, destination=destination)
    else:
        fake.sandbox().fs.copy(source=source, destination=destination)
    assert (tmp_path / destination).read_text() == "file contents"

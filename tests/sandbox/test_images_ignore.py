from __future__ import annotations

from pathlib import Path

import pytest

from baseten.sandbox import ImageIgnoreFileOptions, default_image_ignore_file
from baseten.sandbox._images_ignore import read_ignore_file


@pytest.mark.parametrize(
    ("rel_path", "ignored"),
    [
        (".git", True),
        (".git/config", True),
        ("app/node_modules", True),
        ("app/node_modules/x/y.js", True),
        ("a/b/__pycache__", True),
        ("dist", True),
        ("src/dist", True),
        (".env", True),
        ("app/.env", True),
        (".env.local", True),
        (".envrc", True),
        (".blaxel", True),
        (".env.build", True),
        ("app/.env.build", True),
        # .env* matches at the root only.
        ("app/.env.local", False),
        ("distribution", False),
        ("main.py", False),
        ("src/app.go", False),
        ("Dockerfile", False),
        ("git", False),
    ],
)
def test_default_image_ignore_file(rel_path: str, ignored: bool) -> None:
    options = ImageIgnoreFileOptions(rel_path=rel_path, is_directory=False)
    assert default_image_ignore_file(options) is ignored


def test_reads_a_dockerignore_as_utf8_replacing_invalid_bytes(tmp_path: Path) -> None:
    assert read_ignore_file(str(tmp_path)) is None
    (tmp_path / ".dockerignore").write_bytes(b"node_modules\n\xff\n")
    ignore_file = read_ignore_file(str(tmp_path))
    assert ignore_file is not None
    assert ignore_file.path == str(tmp_path / ".dockerignore")
    assert ignore_file.contents == "node_modules\n�\n"

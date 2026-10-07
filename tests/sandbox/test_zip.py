from __future__ import annotations

import io
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from baseten.sandbox import ImageIgnoreFileOptions
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

# Windows reads every file as 0666, and making links there needs privileges.
_POSIX = sys.platform != "win32"


def _zip(entries: list[ZipEntry]) -> bytes:
    file = io.BytesIO()
    write_zip(entries, file)
    return file.getvalue()


def _tree(root: Path) -> None:
    (root / "Dockerfile").write_text("FROM scratch")
    (root / "run.sh").write_text("#!/bin/sh")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a")


def test_builds_files_entries_sorted_with_mode_0644() -> None:
    entries = files_zip_entries({"b/c.txt": b"\x00", "Dockerfile": "FROM x"})
    assert entries == [
        ZipEntry(path="Dockerfile", data=b"FROM x", mode=0o644),
        ZipEntry(path="b/c.txt", data=b"\x00", mode=0o644),
    ]


def test_files_entries_require_a_dockerfile_and_safe_paths() -> None:
    with pytest.raises(ValueError, match="has no Dockerfile"):
        files_zip_entries({"a": "x"})
    for path in ["/abs", "a//b", "./a", "a/../b", "a\\b"]:
        with pytest.raises(ValueError, match="must be relative"):
            files_zip_entries({"Dockerfile": "x", path: "y"})


def test_zips_entries_read_back_identically_with_modes(tmp_path: Path) -> None:
    local = tmp_path / "local.bin"
    local.write_bytes(b"\x01" * 100_000)
    data = _zip(
        [
            ZipEntry(path="Dockerfile", data=b"FROM x", mode=0o644),
            ZipEntry(path="bin", mode=0o755, is_dir=True),
            ZipEntry(path="bin/tool", mode=0o755, source_path=str(local)),
        ]
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == ["Dockerfile", "bin/", "bin/tool"]
        assert archive.read("bin/tool") == b"\x01" * 100_000
        modes = {info.filename: info.external_attr >> 16 for info in infos}
    assert modes == {
        "Dockerfile": stat.S_IFREG | 0o644,
        "bin/": stat.S_IFDIR | 0o755,
        "bin/tool": stat.S_IFREG | 0o755,
    }


def test_makes_the_same_bytes_for_the_same_entries() -> None:
    entries = files_zip_entries({"Dockerfile": "FROM x", "a.txt": "a" * 1000})
    assert _zip(entries) == _zip(entries)


def test_checks_a_zip_for_a_root_dockerfile_file() -> None:
    check_zip_dockerfile(_zip(files_zip_entries({"Dockerfile": "FROM x"})))
    with pytest.raises(ValueError, match="no Dockerfile file at its root"):
        check_zip_dockerfile(
            _zip(files_zip_entries({"Dockerfile": "x", "sub/Dockerfile": "x"})[1:])
        )
    with pytest.raises(ValueError, match="not a readable zip"):
        check_zip_dockerfile(b"not a zip")


def test_rejects_a_zip_whose_dockerfile_is_a_link() -> None:
    file = io.BytesIO()
    with zipfile.ZipFile(file, "w") as archive:
        info = zipfile.ZipInfo("Dockerfile")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "elsewhere")
    with pytest.raises(ValueError, match="no Dockerfile file"):
        check_zip_dockerfile(file.getvalue())


def test_accepts_a_zip_made_without_unix_modes() -> None:
    file = io.BytesIO()
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr(zipfile.ZipInfo("Dockerfile"), "FROM x")
    check_zip_dockerfile(file.getvalue())


def test_checks_a_directory_for_a_root_dockerfile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="has no Dockerfile at its root"):
        check_directory_dockerfile(str(tmp_path))
    (tmp_path / "Dockerfile").mkdir()
    with pytest.raises(ValueError, match="has no Dockerfile at its root"):
        check_directory_dockerfile(str(tmp_path))


def test_walks_a_directory_sorted_with_modes(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "run.sh").chmod(0o755)
    entries = run_walk(walk_directory(str(tmp_path), "ctx/"), None)
    assert [(entry.path, entry.is_dir) for entry in entries] == [
        ("ctx/Dockerfile", False),
        ("ctx/run.sh", False),
        ("ctx/sub", True),
        ("ctx/sub/a.txt", False),
    ]
    assert entries[3].source_path == str(tmp_path / "sub" / "a.txt")
    if _POSIX:
        assert entries[1].mode == 0o755


@pytest.mark.skipif(not _POSIX, reason="making links needs privileges on Windows")
def test_walks_links_copying_files_within_and_storing_empty_directories(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    (tmp_path / "file-link").symlink_to(tmp_path / "sub" / "a.txt")
    (tmp_path / "dir-link").symlink_to(tmp_path / "sub")
    (tmp_path / "broken-link").symlink_to(tmp_path / "gone")
    entries = {
        entry.path: entry for entry in run_walk(walk_directory(str(tmp_path), ""), None)
    }
    assert "broken-link" not in entries
    assert entries["file-link"].source_path == str(tmp_path / "file-link")
    assert entries["dir-link"].is_dir
    assert not any(path.startswith("dir-link/") for path in entries)


@pytest.mark.skipif(not _POSIX, reason="making links needs privileges on Windows")
def test_fails_a_walk_with_a_link_to_a_file_outside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _tree(root)
    (tmp_path / "secret").write_text("s")
    (root / "leak").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="outside"):
        run_walk(walk_directory(str(root), ""), None)


def test_walk_asks_about_every_path_but_the_root_dockerfile_and_prunes(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    (tmp_path / ".dockerignore").write_text("sub")
    asked: list[ImageIgnoreFileOptions] = []

    def ignore(options: ImageIgnoreFileOptions) -> bool:
        asked.append(options)
        return options.rel_path == "sub"

    entries = run_walk(walk_directory(str(tmp_path), ""), ignore)
    assert [entry.path for entry in entries] == [
        ".dockerignore",
        "Dockerfile",
        "run.sh",
    ]
    assert asked == [
        ImageIgnoreFileOptions(rel_path="run.sh", is_directory=False),
        ImageIgnoreFileOptions(rel_path="sub", is_directory=True),
    ]


def test_walk_fails_when_the_ignore_function_raises(tmp_path: Path) -> None:
    _tree(tmp_path)

    def ignore(_: ImageIgnoreFileOptions) -> bool:
        raise RuntimeError("bad pattern")

    with pytest.raises(RuntimeError, match="bad pattern"):
        run_walk(walk_directory(str(tmp_path), ""), ignore)


@pytest.mark.asyncio
async def test_walks_asynchronously_with_sync_or_async_ignore(tmp_path: Path) -> None:
    _tree(tmp_path)

    async def ignore_async(options: ImageIgnoreFileOptions) -> bool:
        return options.rel_path == "sub"

    entries = await run_walk_async(walk_directory(str(tmp_path), ""), ignore_async)
    assert [entry.path for entry in entries] == ["Dockerfile", "run.sh"]
    entries = await run_walk_async(
        walk_directory(str(tmp_path), ""), lambda options: options.rel_path == "run.sh"
    )
    assert [entry.path for entry in entries] == ["Dockerfile", "sub", "sub/a.txt"]
    entries = await run_walk_async(walk_directory(str(tmp_path), ""), None)
    assert len(entries) == 4

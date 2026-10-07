from __future__ import annotations

import asyncio
import inspect
import io
import os
import shutil
import stat
import zipfile
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import IO

from baseten.sandbox._images_ignore import (
    DOCKERIGNORE_FILE_NAME,
    AsyncImageIgnoreFileFunc,
    ImageIgnoreFileFunc,
    ImageIgnoreFileOptions,
)

# Asks for an ignore decision on each path, and gets the entries at the end.
DirectoryWalk = Generator[ImageIgnoreFileOptions, bool, "list[ZipEntry]"]

# The MS-DOS directory attribute, set alongside the Unix mode on a directory.
_MSDOS_DIRECTORY = 0x10

# The creator system that tells readers the high bits of an entry's external
# attributes hold a Unix mode.
_CREATOR_UNIX = 3


@dataclass(kw_only=True)
class ZipEntry:
    """One file or directory of a build context."""

    path: str
    """Path in the zip, with forward slashes and no trailing slash."""

    mode: int
    """Unix permission bits."""

    is_dir: bool = False

    data: bytes = b""
    """A file's content, unless ``source_path`` is set."""

    source_path: str | None = None
    """A local file read while zipping, so a large build context is never held in memory whole."""


def write_zip(entries: list[ZipEntry], file: IO[bytes]) -> None:
    """Zip entries, in order, into a file."""
    with zipfile.ZipFile(file, "w") as archive:
        for entry in entries:
            if entry.is_dir:
                info = zipfile.ZipInfo(entry.path + "/")
                info.external_attr = (
                    (stat.S_IFDIR | entry.mode) << 16
                ) | _MSDOS_DIRECTORY
            else:
                info = zipfile.ZipInfo(entry.path)
                info.external_attr = (stat.S_IFREG | entry.mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _CREATOR_UNIX
            if entry.source_path is None:
                archive.writestr(info, entry.data)
                continue
            with open(entry.source_path, "rb") as source:
                # ZIP64 must be chosen before writing an entry of unknown
                # size, so it goes by the file's size now.
                large = os.fstat(source.fileno()).st_size >= zipfile.ZIP64_LIMIT
                with archive.open(info, "w", force_zip64=large) as target:
                    shutil.copyfileobj(source, target)


def check_zip_dockerfile(data: bytes) -> None:
    """Check that a caller's zip has a ``Dockerfile`` file at its root."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise ValueError(f"the zip source is not a readable zip: {err}") from err
    with archive:
        for info in archive.infolist():
            file_type = stat.S_IFMT(info.external_attr >> 16)
            # A link named Dockerfile would only fail later, in the build. A
            # type of 0 is a zip made without Unix modes.
            if (
                info.filename == "Dockerfile"
                and not info.is_dir()
                and file_type in (0, stat.S_IFREG)
            ):
                return
    raise ValueError("the zip source has no Dockerfile file at its root")


def files_zip_entries(files: Mapping[str, str | bytes]) -> list[ZipEntry]:
    """Build entries from in-memory files, each with mode 0644."""
    if "Dockerfile" not in files:
        raise ValueError("the files source has no Dockerfile")
    entries: list[ZipEntry] = []
    # Sorted, so the same files always zip the same way.
    for path in sorted(files):
        segments = path.split("/")
        if "\\" in path or any(segment in ("", ".", "..") for segment in segments):
            raise ValueError(
                f"files path {path!r} must be relative, with forward slashes and "
                "no empty, . or .. segments"
            )
        content = files[path]
        data = content.encode() if isinstance(content, str) else content
        entries.append(ZipEntry(path=path, data=data, mode=0o644))
    return entries


def check_directory_dockerfile(directory: str) -> None:
    """Check that a directory has a ``Dockerfile`` file at its root."""
    # Checked before reading anything, so a mistaken directory, such as a home
    # directory, fails fast instead of being read in full.
    if not os.path.isfile(os.path.join(directory, "Dockerfile")):
        raise ValueError(f"directory {directory} has no Dockerfile at its root")


def walk_directory(directory: str, prefix: str) -> DirectoryWalk:
    """Walk a local directory into entries, each path prefixed with ``prefix``.

    Yields every path but the root ``Dockerfile`` and ``.dockerignore``, which
    Docker always keeps in a build context, and leaves out each one it is sent
    true for. Files keep their modes, a link to a file within the directory
    stores a copy of the file, a link to a file outside it is an error, a link
    to a directory stores an empty directory, and a broken link is left out.
    """
    # Resolved, so a link's resolved target can be checked against it.
    root = os.path.realpath(directory)
    entries: list[ZipEntry] = []
    yield from _walk_tree(root, directory, "", prefix, entries)
    return entries


def _walk_tree(
    root: str, directory: str, rel_dir: str, prefix: str, entries: list[ZipEntry]
) -> Generator[ImageIgnoreFileOptions, bool, None]:
    """Walk the children of a directory whose path relative to the walked one is ``rel_dir``."""
    with os.scandir(directory) as scan:
        # Sorted by name, so the same directory always zips the same way.
        children = sorted(scan, key=lambda child: child.name)
    for child in children:
        rel_path = rel_dir + child.name
        always_kept = rel_dir == "" and child.name in (
            "Dockerfile",
            DOCKERIGNORE_FILE_NAME,
        )
        if not always_kept:
            ignored = yield ImageIgnoreFileOptions(
                rel_path=rel_path, is_directory=child.is_dir(follow_symlinks=False)
            )
            if ignored:
                continue
        archive_path = prefix + rel_path
        try:
            info = os.stat(child.path)
        except FileNotFoundError:
            if child.is_symlink():
                # A broken link has nothing to archive.
                continue
            raise
        mode = stat.S_IMODE(info.st_mode) & 0o777
        if stat.S_ISDIR(info.st_mode):
            entries.append(ZipEntry(path=archive_path, mode=mode, is_dir=True))
            # A link to a directory is stored as an empty directory, not
            # followed, so a link cannot pull in the rest of the machine.
            if not child.is_symlink():
                yield from _walk_tree(root, child.path, rel_path + "/", prefix, entries)
        elif stat.S_ISREG(info.st_mode):
            # A link to a file is stored as a copy of the file, but only one
            # within the directory, so a link cannot pull in a file from
            # elsewhere on the machine.
            if child.is_symlink():
                target = os.path.realpath(child.path)
                if not _is_within(root, target):
                    raise ValueError(
                        f"{child.path} is a link to {target}, outside {root}; only "
                        "links to files within it can be pushed"
                    )
            entries.append(
                ZipEntry(path=archive_path, mode=mode, source_path=child.path)
            )
        # Anything else, such as a socket, has no content to archive.


def _is_within(root: str, path: str) -> bool:
    """Report whether a resolved path is inside a resolved root."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        # On another Windows drive.
        return False
    return (
        relative != os.pardir
        and not relative.startswith(os.pardir + os.sep)
        and not os.path.isabs(relative)
    )


def run_walk(walk: DirectoryWalk, ignore: ImageIgnoreFileFunc | None) -> list[ZipEntry]:
    """Run a directory walk, deciding each path with ``ignore``, which keeps all when ``None``."""
    try:
        options = next(walk)
        while True:
            options = walk.send(ignore is not None and ignore(options))
    except StopIteration as stop:
        return stop.value


async def run_walk_async(
    walk: DirectoryWalk, ignore: AsyncImageIgnoreFileFunc | None
) -> list[ZipEntry]:
    """Async form of :func:`run_walk`, reading the filesystem on a worker thread."""
    done, step = await asyncio.to_thread(_walk_step, walk, None)
    while not done:
        assert isinstance(step, ImageIgnoreFileOptions)
        ignored = False
        if ignore is not None:
            result = ignore(step)
            ignored = await result if inspect.isawaitable(result) else result
        done, step = await asyncio.to_thread(_walk_step, walk, ignored)
    assert isinstance(step, list)
    return step


def _walk_step(
    walk: DirectoryWalk, ignored: bool | None
) -> tuple[bool, ImageIgnoreFileOptions | list[ZipEntry]]:
    """Advance a walk, giving whether it is done and its next path or its entries."""
    # Caught here, since a StopIteration cannot cross into a future.
    try:
        return False, next(walk) if ignored is None else walk.send(ignored)
    except StopIteration as stop:
        return True, stop.value

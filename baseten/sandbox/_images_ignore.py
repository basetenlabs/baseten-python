from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias

DOCKERIGNORE_FILE_NAME = ".dockerignore"


@dataclass(kw_only=True)
class ImageIgnoreFileOptions:
    """A candidate path in a directory being pushed, for an ignore function."""

    rel_path: str
    """Path relative to the directory's root, with forward slashes on all platforms."""

    is_directory: bool
    """Whether the path is a directory, not a link to one."""


ImageIgnoreFileFunc: TypeAlias = Callable[[ImageIgnoreFileOptions], bool]
"""Reports whether a path should be left out of a pushed build context.

When it returns true for a directory, the directory's entire subtree is left
out. Raising fails the push before anything is sent.
"""

AsyncImageIgnoreFileFunc: TypeAlias = Callable[
    [ImageIgnoreFileOptions], bool | Awaitable[bool]
]
"""Async form of :data:`ImageIgnoreFileFunc`, which may also be a coroutine function."""


@dataclass(kw_only=True)
class ImageIgnoreFileProcessorOptions:
    """A ``.dockerignore`` found at the root of a directory being pushed."""

    path: str
    """Absolute path of the ``.dockerignore``."""

    contents: str
    """Contents of the ``.dockerignore``, decoded as UTF-8."""


ImageIgnoreFileProcessor: TypeAlias = Callable[
    [ImageIgnoreFileProcessorOptions], ImageIgnoreFileFunc
]
"""Parses a ``.dockerignore`` into an :data:`ImageIgnoreFileFunc`.

The function should match Docker's ``.dockerignore`` semantics.
"""

AsyncImageIgnoreFileProcessor: TypeAlias = Callable[
    [ImageIgnoreFileProcessorOptions],
    AsyncImageIgnoreFileFunc | Awaitable[AsyncImageIgnoreFileFunc],
]
"""Async form of :data:`ImageIgnoreFileProcessor`, which may also be a coroutine function."""

# Left out at any depth by default_image_ignore_file.
_DEFAULT_IGNORED_NAMES = frozenset(
    {
        ".blaxel",
        ".env.build",
        ".docker",
        ".git",
        "dist",
        ".venv",
        "venv",
        "node_modules",
        ".env",
        ".next",
        "__pycache__",
    }
)


def default_image_ignore_file(options: ImageIgnoreFileOptions) -> bool:
    """Report whether a path is left out of a pushed build context by default.

    Applied when the directory has no ``.dockerignore``. Its result is the same
    as this ``.dockerignore``::

        **/.blaxel
        **/.env.build
        **/.docker
        **/.git
        **/dist
        **/.venv
        **/venv
        **/node_modules
        **/.env
        .env*
        **/.next
        **/__pycache__

    So each of those names is left out at any depth, as a file or as a
    directory with everything in it, and a name starting with ``.env`` is left
    out at the root. To extend the defaults, copy them into a
    ``.dockerignore``.

    Args:
        options: The candidate path.
    """
    components = options.rel_path.split("/")
    # Any component, so a path under an ignored directory is ignored even when
    # its directory was not pruned first.
    if any(component in _DEFAULT_IGNORED_NAMES for component in components):
        return True
    # .env* has no **/, so it matches at the root only, as Docker reads it.
    return components[0].startswith(".env")


def read_ignore_file(directory: str) -> ImageIgnoreFileProcessorOptions | None:
    """Read the ``.dockerignore`` at a directory's root, or ``None`` without one."""
    path = os.path.abspath(os.path.join(directory, DOCKERIGNORE_FILE_NAME))
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            contents = file.read()
    except FileNotFoundError:
        return None
    return ImageIgnoreFileProcessorOptions(path=path, contents=contents)


def missing_processor_error(ignore_file: ImageIgnoreFileProcessorOptions) -> ValueError:
    """Give the error for a ``.dockerignore`` with no processor to read it."""
    # Failing, rather than pushing everything, keeps files the caller meant to
    # leave out, such as secrets, from being uploaded.
    return ValueError(f"{ignore_file.path} exists but ignore_file_processor is not set")

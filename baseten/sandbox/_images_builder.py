from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, Self

from baseten.sandbox._zip import ZipEntry, run_walk, walk_directory

_DEFAULT_SANDBOX_API_IMAGE = "ghcr.io/blaxel-ai/sandbox:latest"
_SANDBOX_API_PATH = "/usr/local/bin/sandbox-api"

# Names the build itself reads at the root of its context, so a context entry
# must never take them.
_RESERVED_CONTEXT_NAMES = frozenset({"Dockerfile", ".dockerignore"})

# Characters a package manager argument may have and still be passed to the
# shell unquoted.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9._\-+=:/@~^*]+")

_ENTRYPOINT = re.compile(r"\s*ENTRYPOINT\b", re.IGNORECASE)


@dataclass(frozen=True, kw_only=True)
class _ContextEntry:
    """A file or directory the builder adds to the build context."""

    kind: Literal["content", "file", "directory"]
    name: str
    content: bytes = b""
    mode: int = 0o644
    source_path: str = ""


@dataclass(frozen=True, kw_only=True)
class _BuilderState:
    base_image: str
    sandbox_api_image: str
    instructions: tuple[str, ...] = ()
    context: tuple[_ContextEntry, ...] = ()


class ImageBuilder:
    """An image described as a Dockerfile and the files it copies in.

    Push it with :meth:`ImageClient.push`. Start one with
    :meth:`ImageBuilder.from_registry`.

    A builder never changes: each method returns a new builder with the
    instruction appended, so one builder can safely be the base of several.
    Invalid input raises :class:`ValueError` at the call that takes it.

    The Dockerfile it pushes ends by copying in the sandbox API binary and
    making it the entrypoint, unless the builder already did either, since a
    sandbox needs it running to be reached. :meth:`dockerfile` shows the
    Dockerfile exactly as pushed.
    """

    def __init__(self, state: _BuilderState) -> None:
        """Internal. Use :meth:`from_registry` instead.

        :meta private:
        """
        self._state = state

    @classmethod
    def from_registry(
        cls, base_image: str, *, sandbox_api_image: str | None = None
    ) -> ImageBuilder:
        """Start a builder from a registry image, as ``FROM <base_image>``.

        Args:
            base_image: Registry image to start from.
            sandbox_api_image: Image to copy the sandbox API binary from, when
                the Dockerfile does not bring it in itself. Defaults to
                ``ghcr.io/blaxel-ai/sandbox:latest``.
        """
        return cls(
            _BuilderState(
                base_image=_single_line("base_image", base_image),
                sandbox_api_image=_single_line(
                    "sandbox_api_image",
                    _DEFAULT_SANDBOX_API_IMAGE
                    if sandbox_api_image is None
                    else sandbox_api_image,
                ),
            )
        )

    @property
    def base_image(self) -> str:
        """The image this builder starts from."""
        return self._state.base_image

    def dockerfile(self) -> str:
        """Give the Dockerfile, exactly as it is pushed."""
        state = self._state
        lines = [f"FROM {state.base_image}", *state.instructions]
        text = "\n".join(lines)
        if "sandbox-api" not in text and "blaxel-ai/sandbox" not in text:
            lines.append(
                f"COPY --from={state.sandbox_api_image} /sandbox-api {_SANDBOX_API_PATH}"
            )
        # Read from the lines rather than tracked by entrypoint(), so one
        # written with dockerfile_lines() counts too.
        has_entrypoint = any(
            _ENTRYPOINT.match(line)
            for instruction in state.instructions
            for line in instruction.split("\n")
        )
        if not has_entrypoint:
            lines.append(f"ENTRYPOINT {_json([_SANDBOX_API_PATH])}")
        return "\n".join(lines) + "\n"

    def workdir(self, path: str) -> ImageBuilder:
        """Append ``WORKDIR <path>``."""
        return self._append(f"WORKDIR {_single_line('path', path)}")

    def run_commands(self, *commands: str) -> ImageBuilder:
        """Append ``RUN <command>`` for each command, in shell form."""
        return self._append(
            *(f"RUN {_single_line('command', command)}" for command in commands)
        )

    def env(self, variables: Mapping[str, str]) -> ImageBuilder:
        """Append ``ENV <name>="<value>"`` for each variable.

        ``"`` and ``\\`` in the value are escaped. ``$`` is left as is, so the
        build expands variables in it, as in ``{"PATH": "/opt/bin:$PATH"}``.
        """
        return self._append(
            *(
                f"ENV {_key_name('env name', name)}={_quoted('env value', value)}"
                for name, value in variables.items()
            )
        )

    def copy(self, source: str, destination: str) -> ImageBuilder:
        """Append ``COPY ["<source>", "<destination>"]``, copying from the build context."""
        _single_line("source", source)
        _single_line("destination", destination)
        return self._append(f"COPY {_json([source, destination])}")

    def expose(self, *ports: int) -> ImageBuilder:
        """Append ``EXPOSE <port>`` for each port."""
        for port in ports:
            if not 1 <= port <= 65535:
                raise ValueError(f"port {port} is not between 1 and 65535")
        return self._append(*(f"EXPOSE {port}" for port in ports))

    def entrypoint(self, *args: str) -> ImageBuilder:
        """Append ``ENTRYPOINT ["<arg>", ...]``, in exec form.

        The image then runs this instead of the sandbox API binary, so it must
        start that itself for the sandbox to be reachable.
        """
        if not args:
            return self
        for arg in args:
            _single_line("entrypoint argument", arg)
        return self._append(f"ENTRYPOINT {_json(list(args))}")

    def user(self, user: str) -> ImageBuilder:
        """Append ``USER <user>``."""
        return self._append(f"USER {_single_line('user', user)}")

    def label(self, labels: Mapping[str, str]) -> ImageBuilder:
        """Append ``LABEL <key>="<value>"`` for each label, escaped as in :meth:`env`."""
        return self._append(
            *(
                f"LABEL {_key_name('label key', key)}={_quoted('label value', value)}"
                for key, value in labels.items()
            )
        )

    def arg(self, name: str, default: str | None = None) -> ImageBuilder:
        """Append ``ARG <name>``, or ``ARG <name>="<default>"`` escaped as in :meth:`env`."""
        declared = _key_name("arg name", name)
        if default is None:
            return self._append(f"ARG {declared}")
        return self._append(f"ARG {declared}={_quoted('arg default', default)}")

    def dockerfile_lines(self, *lines: str) -> ImageBuilder:
        """Append each line verbatim.

        For anything the other methods do not cover, such as comments, ``CMD``,
        or a heredoc. A line may contain newlines, and nothing is checked or
        escaped.
        """
        return self._append(*lines)

    def pip_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN pip install <args>``."""
        return self._run("pip install", args)

    def apt_install(self, *args: str) -> ImageBuilder:
        """Append an ``apt-get install`` of the packages as one step.

        The step is ``RUN apt-get update && apt-get install -y
        --no-install-recommends <args> && rm -rf /var/lib/apt/lists/*``, so the
        package lists add nothing to the image.
        """
        if not args:
            return self
        return self._append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{_shell_args(args)} && rm -rf /var/lib/apt/lists/*"
        )

    def apk_add(self, *args: str) -> ImageBuilder:
        """Append ``RUN apk add --no-cache <args>``."""
        return self._run("apk add --no-cache", args)

    def npm_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN npm install <args>``.

        With no arguments, appends ``RUN npm install``, to install from the
        working directory's ``package.json``.
        """
        if not args:
            return self._append("RUN npm install")
        return self._run("npm install", args)

    def gem_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN gem install --no-document <args>``."""
        return self._run("gem install --no-document", args)

    def cargo_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN cargo install <args>``."""
        return self._run("cargo install", args)

    def go_install(self, *packages: str) -> ImageBuilder:
        """Append ``RUN go install <package> && go install <package> ...``.

        One ``go install`` per package, since one call can only install
        packages from a single module.
        """
        return self._run_each("go install", packages)

    def composer_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN composer require <args>``."""
        return self._run("composer require", args)

    def uv_install(self, *args: str) -> ImageBuilder:
        """Append ``RUN uv pip install --system <args>``."""
        return self._run("uv pip install --system", args)

    def pipx_install(self, *packages: str) -> ImageBuilder:
        """Append ``RUN pipx install <package> && pipx install <package> ...``, one per package."""
        return self._run_each("pipx install", packages)

    def add_local_file(
        self, source_path: str, destination: str, *, context_name: str | None = None
    ) -> ImageBuilder:
        """Add a local file to the build context and append ``COPY ["<name>", "<destination>"]``.

        The name is the file's name in the context. The file is read when
        pushed, keeping its permissions, and a symbolic link is read as the
        file it points to.

        Args:
            source_path: Local file to add.
            destination: Path to copy it to in the image.
            context_name: Name in the build context. Defaults to the source's
                basename, with a suffix such as `` (1)`` if another entry
                already has that name.
        """
        return self._add_local("file", source_path, destination, context_name)

    def add_local_dir(
        self, source_path: str, destination: str, *, context_name: str | None = None
    ) -> ImageBuilder:
        """Add a local directory to the build context and append ``COPY ["<name>", "<destination>"]``.

        The name is the directory's name in the context. As with any ``COPY``
        of a directory, its contents are copied into the destination, not the
        directory itself. It is read when pushed, with files keeping their
        permissions, a link to a file within the directory stored as a copy of
        the file, a link to a file outside it failing the push, and a link to
        a directory stored as an empty directory. Everything in it is added: no
        ``.dockerignore`` or default ignore rules apply.

        Args:
            source_path: Local directory to add.
            destination: Path to copy its contents to in the image.
            context_name: Name in the build context. Defaults to the source's
                basename, with a suffix such as `` (1)`` if another entry
                already has that name.
        """
        return self._add_local("directory", source_path, destination, context_name)

    def add_file(
        self,
        destination: str,
        content: str | bytes,
        *,
        context_name: str | None = None,
        mode: int = 0o644,
    ) -> ImageBuilder:
        """Add a file with the given content to the build context and append ``COPY ["<name>", "<destination>"]``.

        The name is the file's name in the context.

        Args:
            destination: Path of the file in the image.
            content: Content of the file. A string is written as UTF-8.
            context_name: Name in the build context. Defaults to the
                destination's basename, with a suffix such as `` (1)`` if
                another entry already has that name.
            mode: Unix permission bits of the file in the image.
        """
        _single_line("destination", destination)
        default_name = destination.split("/")[-1]
        if context_name is None and default_name == "":
            raise ValueError(
                f"destination {destination} has no file name; set context_name"
            )
        name = self._context_name(context_name, default_name)
        return self._append(
            f"COPY {_json([name, destination])}",
            context_entry=_ContextEntry(
                kind="content",
                name=name,
                content=content.encode() if isinstance(content, str) else content,
                mode=mode,
            ),
        )

    def _zip_entries(self) -> list[ZipEntry]:
        """Give the build context to zip, with local entries checked now and read while zipping."""
        entries = [
            ZipEntry(path="Dockerfile", data=self.dockerfile().encode(), mode=0o644)
        ]
        for entry in self._state.context:
            if entry.kind == "content":
                entries.append(
                    ZipEntry(path=entry.name, data=entry.content, mode=entry.mode)
                )
                continue
            try:
                info = os.stat(entry.source_path)
            except FileNotFoundError:
                info = None
            if entry.kind == "file":
                if info is None or not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"local file {entry.source_path} is not a file")
                entries.append(
                    ZipEntry(
                        path=entry.name,
                        mode=stat.S_IMODE(info.st_mode) & 0o777,
                        source_path=entry.source_path,
                    )
                )
                continue
            if info is None or not stat.S_ISDIR(info.st_mode):
                raise ValueError(
                    f"local directory {entry.source_path} is not a directory"
                )
            entries.append(
                ZipEntry(
                    path=entry.name,
                    mode=stat.S_IMODE(info.st_mode) & 0o777,
                    is_dir=True,
                )
            )
            # No ignore rules: adding a directory is an explicit choice of its
            # files, as Docker's COPY of a directory takes them all.
            entries.extend(
                run_walk(walk_directory(entry.source_path, entry.name + "/"), None)
            )
        return entries

    def _append(
        self, *instructions: str, context_entry: _ContextEntry | None = None
    ) -> Self:
        """Give a new builder with the instructions and context entry appended."""
        if not instructions:
            return self
        state = self._state
        return type(self)(
            replace(
                state,
                instructions=state.instructions + instructions,
                context=state.context
                if context_entry is None
                else (*state.context, context_entry),
            )
        )

    def _run(self, command: str, args: tuple[str, ...]) -> ImageBuilder:
        """Append ``RUN <command> <args>``, or nothing without arguments."""
        if not args:
            return self
        return self._append(f"RUN {command} {_shell_args(args)}")

    def _run_each(self, command: str, packages: tuple[str, ...]) -> ImageBuilder:
        """Append one ``RUN`` running the command once per package."""
        if not packages:
            return self
        runs = [f"{command} {_shell_args((package,))}" for package in packages]
        return self._append("RUN " + " && ".join(runs))

    def _add_local(
        self,
        kind: Literal["file", "directory"],
        source_path: str,
        destination: str,
        context_name: str | None,
    ) -> ImageBuilder:
        """Add a local file or directory, read when pushed."""
        _single_line("destination", destination)
        # Resolved now, so a later change of working directory does not change
        # what is pushed.
        resolved = os.path.abspath(source_path)
        name = self._context_name(context_name, os.path.basename(resolved))
        return self._append(
            f"COPY {_json([name, destination])}",
            context_entry=_ContextEntry(kind=kind, name=name, source_path=resolved),
        )

    def _context_name(self, requested: str | None, default_name: str) -> str:
        """Give a context entry's name.

        An explicit name is used exactly or raises; a default one is suffixed
        the way browsers name a repeated download, before any extension.
        """
        taken = {entry.name for entry in self._state.context} | _RESERVED_CONTEXT_NAMES
        if requested is not None:
            _check_context_name(requested)
            if requested in taken:
                raise ValueError(
                    f"context name {requested} is reserved or already used"
                )
            return requested
        _check_context_name(default_name)
        if default_name not in taken:
            return default_name
        dot = default_name.rfind(".")
        stem, extension = (
            (default_name[:dot], default_name[dot:]) if dot > 0 else (default_name, "")
        )
        number = 1
        while f"{stem} ({number}){extension}" in taken:
            number += 1
        return f"{stem} ({number}){extension}"


def _single_line(what: str, value: str) -> str:
    """Raise if a value would span lines in the Dockerfile, otherwise return it."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} must not contain a newline")
    return value


def _key_name(what: str, value: str) -> str:
    """Check an env, label, or arg name, which is written unquoted."""
    if value == "" or re.search(r'[\s="]', value):
        raise ValueError(f'{what} {value!r} must be non-empty, without spaces, = or "')
    return value


def _quoted(what: str, value: str) -> str:
    """Double-quote a value, escaping ``"`` and ``\\``."""
    return '"' + re.sub(r'(["\\])', r"\\\1", _single_line(what, value)) + '"'


def _check_context_name(name: str) -> None:
    """Raise unless a context name is one path segment."""
    if name in ("", ".", "..") or re.search(r"[/\\\r\n]", name):
        raise ValueError(
            f"context name {name!r} must be one path segment, without slashes or newlines"
        )


def _shell_args(args: tuple[str, ...]) -> str:
    """Join arguments for the shell, single-quoting any that need it."""
    quoted = []
    for arg in args:
        _single_line("argument", arg)
        quoted.append(
            arg if _SHELL_SAFE.fullmatch(arg) else "'" + arg.replace("'", "'\\''") + "'"
        )
    return " ".join(quoted)


def _json(values: list[str]) -> str:
    """Write a JSON array as a Dockerfile's exec form takes it, compact like JavaScript's."""
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

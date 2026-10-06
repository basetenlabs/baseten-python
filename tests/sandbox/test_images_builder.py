from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from baseten.sandbox import ImageBuilder
from baseten.sandbox._zip import ZipEntry

_INJECTED = [
    "COPY --from=ghcr.io/blaxel-ai/sandbox:latest /sandbox-api /usr/local/bin/sandbox-api",
    'ENTRYPOINT ["/usr/local/bin/sandbox-api"]',
]

# Windows reads every file as 0666 and has no exec bits to keep.
_KEEPS_MODES = sys.platform != "win32"


def _expected(lines: list[str], base: str = "debian:bookworm-slim") -> str:
    # The Dockerfile a builder from base pushes with lines, injection included.
    return "\n".join([f"FROM {base}", *lines, *_INJECTED]) + "\n"


def _base() -> ImageBuilder:
    return ImageBuilder.from_registry("debian:bookworm-slim")


def test_writes_each_core_instruction_then_injects_the_sandbox_api() -> None:
    builder = (
        _base()
        .workdir("/app")
        .run_commands("echo one", "echo two")
        .env({"A": "1", "B": "two words"})
        .copy("src dir", "/app/src")
        .expose(8080, 9090)
        .user("app")
        .label({"team": "infra"})
        .arg("VERSION")
        .arg("CHANNEL", "stable")
    )
    assert builder.base_image == "debian:bookworm-slim"
    assert builder.dockerfile() == _expected(
        [
            "WORKDIR /app",
            "RUN echo one",
            "RUN echo two",
            'ENV A="1"',
            'ENV B="two words"',
            'COPY ["src dir","/app/src"]',
            "EXPOSE 8080",
            "EXPOSE 9090",
            "USER app",
            'LABEL team="infra"',
            "ARG VERSION",
            'ARG CHANNEL="stable"',
        ]
    )


def test_escapes_quotes_and_backslashes_in_values_leaving_dollar() -> None:
    builder = (
        _base()
        .env({"PATH": "/opt/bin:$PATH", "QUOTE": 'say "hi" \\ bye'})
        .label({"note": 'a "b"'})
        .arg("X", 'c"d')
    )
    assert builder.dockerfile() == _expected(
        [
            'ENV PATH="/opt/bin:$PATH"',
            'ENV QUOTE="say \\"hi\\" \\\\ bye"',
            'LABEL note="a \\"b\\""',
            'ARG X="c\\"d"',
        ]
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: ImageBuilder.from_registry("a\nb"), "base_image must not"),
        (lambda: _base().workdir("/a\n/b"), "path must not"),
        (lambda: _base().run_commands("a\nb"), "command must not"),
        (lambda: _base().env({"A": "1\r\n2"}), "env value must not"),
        (lambda: _base().copy("a\n", "/b"), "source must not"),
        (lambda: _base().entrypoint("a\nb"), "entrypoint argument must not"),
        (lambda: _base().pip_install("a\nb"), "argument must not"),
    ],
)
def test_raises_on_a_newline_anywhere_but_raw_lines(
    build: Callable[[], ImageBuilder], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_raises_on_a_name_that_cannot_be_written_unquoted() -> None:
    with pytest.raises(ValueError, match="env name 'A B'"):
        _base().env({"A B": "1"})
    with pytest.raises(ValueError, match="label key 'a=b'"):
        _base().label({"a=b": "1"})
    with pytest.raises(ValueError, match="arg name ''"):
        _base().arg("")


def test_rejects_ports_that_are_not_between_1_and_65535() -> None:
    with pytest.raises(ValueError, match="port 0 is not between 1 and 65535"):
        _base().expose(0)
    with pytest.raises(ValueError, match="port 65536"):
        _base().expose(65536)


def test_appends_raw_lines_verbatim_newlines_included() -> None:
    heredoc = "RUN <<EOF\necho one\necho two\nEOF"
    builder = _base().dockerfile_lines("# a comment", heredoc)
    assert builder.dockerfile() == _expected(["# a comment", heredoc])


def test_writes_an_entrypoint_in_exec_form_instead_of_the_default() -> None:
    builder = _base().entrypoint("/bin/sh", "-c", 'echo "hi"')
    assert builder.dockerfile() == (
        "\n".join(
            [
                "FROM debian:bookworm-slim",
                'ENTRYPOINT ["/bin/sh","-c","echo \\"hi\\""]',
                _INJECTED[0],
            ]
        )
        + "\n"
    )
    assert _base().entrypoint().dockerfile() == _expected([])


def test_counts_an_entrypoint_in_raw_lines_in_any_case_but_not_in_a_comment() -> None:
    assert (
        _INJECTED[1] not in _base().dockerfile_lines('entrypoint ["/run"]').dockerfile()
    )
    assert (
        _INJECTED[1] in _base().dockerfile_lines('# ENTRYPOINT ["/run"]').dockerfile()
    )


def test_skips_copying_the_sandbox_api_when_already_brought_in() -> None:
    assert (
        _INJECTED[0]
        not in _base()
        .run_commands("curl -o /usr/local/bin/sandbox-api https://x")
        .dockerfile()
    )
    assert (
        _INJECTED[0]
        not in ImageBuilder.from_registry("ghcr.io/blaxel-ai/sandbox:v1").dockerfile()
    )


def test_copies_the_sandbox_api_from_the_given_image() -> None:
    builder = ImageBuilder.from_registry(
        "debian:bookworm-slim", sandbox_api_image="registry.example/sandbox:v2"
    )
    assert (
        "COPY --from=registry.example/sandbox:v2 /sandbox-api /usr/local/bin/sandbox-api"
        in builder.dockerfile()
    )


def test_never_changes_each_call_returns_a_new_builder() -> None:
    shared = _base().pip_install("numpy")
    with_torch = shared.pip_install("torch")
    with_jax = shared.pip_install("jax")
    assert shared.dockerfile() == _expected(["RUN pip install numpy"])
    assert with_torch.dockerfile() == _expected(
        ["RUN pip install numpy", "RUN pip install torch"]
    )
    assert with_jax.dockerfile() == _expected(
        ["RUN pip install numpy", "RUN pip install jax"]
    )


def test_writes_each_package_managers_command_quoting_arguments() -> None:
    builder = (
        _base()
        .pip_install("--pre", "numpy>=2", "it's")
        .apt_install("git", "curl")
        .apk_add("git")
        .npm_install("-g", "typescript@5")
        .npm_install()
        .gem_install("rails")
        .cargo_install("--locked", "ripgrep")
        .go_install("golang.org/x/tools/gopls@latest", "example.com/cmd@v1")
        .composer_install("laravel/framework")
        .uv_install("ruff")
        .pipx_install("black", "httpie")
    )
    assert builder.dockerfile() == _expected(
        [
            "RUN pip install --pre 'numpy>=2' 'it'\\''s'",
            "RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*",
            "RUN apk add --no-cache git",
            "RUN npm install -g typescript@5",
            "RUN npm install",
            "RUN gem install --no-document rails",
            "RUN cargo install --locked ripgrep",
            "RUN go install golang.org/x/tools/gopls@latest && go install example.com/cmd@v1",
            "RUN composer require laravel/framework",
            "RUN uv pip install --system ruff",
            "RUN pipx install black && pipx install httpie",
        ]
    )


def test_appends_nothing_for_a_package_manager_without_arguments_except_npm() -> None:
    builder = (
        _base()
        .pip_install()
        .apt_install()
        .apk_add()
        .gem_install()
        .cargo_install()
        .go_install()
        .composer_install()
        .uv_install()
        .pipx_install()
        .run_commands()
        .env({})
        .expose()
    )
    assert builder.dockerfile() == _expected([])


def test_names_context_entries_by_basename_suffixing_repeats_and_reserved() -> None:
    builder = (
        _base()
        .add_file("/etc/a/config.json", "1")
        .add_file("/etc/b/config.json", "2")
        .add_file("/etc/c/config.json", "3")
        .add_file("/srv/Makefile", "4")
        .add_file("/opt/Makefile", "5")
        .add_file("/x/Dockerfile", "6")
        .add_file("/x/.dockerignore", "7")
    )
    assert builder.dockerfile() == _expected(
        [
            'COPY ["config.json","/etc/a/config.json"]',
            'COPY ["config (1).json","/etc/b/config.json"]',
            'COPY ["config (2).json","/etc/c/config.json"]',
            'COPY ["Makefile","/srv/Makefile"]',
            'COPY ["Makefile (1)","/opt/Makefile"]',
            'COPY ["Dockerfile (1)","/x/Dockerfile"]',
            'COPY [".dockerignore (1)","/x/.dockerignore"]',
        ]
    )


def test_uses_an_explicit_context_name_exactly_raising_if_taken_or_reserved() -> None:
    builder = _base().add_file("/app/run.sh", "x", context_name="start.sh")
    assert 'COPY ["start.sh","/app/run.sh"]' in builder.dockerfile()
    with pytest.raises(
        ValueError, match="context name start.sh is reserved or already used"
    ):
        builder.add_file("/b", "y", context_name="start.sh")
    with pytest.raises(ValueError, match="context name Dockerfile is reserved"):
        _base().add_file("/b", "y", context_name="Dockerfile")
    with pytest.raises(ValueError, match="context name 'a/b' must be one path segment"):
        _base().add_file("/b", "y", context_name="a/b")


def test_needs_a_context_name_for_a_destination_ending_in_a_slash() -> None:
    with pytest.raises(ValueError, match="destination /app/ has no file name"):
        _base().add_file("/app/", "x")
    assert (
        'COPY ["x.txt","/app/"]'
        in _base().add_file("/app/", "x", context_name="x.txt").dockerfile()
    )


def test_zips_the_dockerfile_and_every_entry_reading_local_files_at_push(
    tmp_path: Path,
) -> None:
    tool = tmp_path / "tool.sh"
    tool.write_text("before")
    tool.chmod(0o755)
    (tmp_path / "conf" / "nested").mkdir(parents=True)
    (tmp_path / "conf" / "nested" / "a.txt").write_text("a")
    builder = (
        _base()
        .add_file("/etc/text.txt", "text")
        .add_file("/etc/bytes.bin", b"\x00\xff")
        .add_file("/usr/local/bin/run", b"#!/bin/sh", mode=0o755)
        .add_local_file(str(tool), "/usr/local/bin/tool")
        .add_local_dir(str(tmp_path / "conf"), "/etc/conf")
    )
    # Read when pushed, not when added.
    tool.write_text("after")
    entries = {entry.path: entry for entry in builder._zip_entries()}
    assert list(entries) == [
        "Dockerfile",
        "text.txt",
        "bytes.bin",
        "run",
        "tool.sh",
        "conf",
        "conf/nested",
        "conf/nested/a.txt",
    ]
    assert entries["Dockerfile"].data == builder.dockerfile().encode()
    assert entries["text.txt"] == ZipEntry(path="text.txt", data=b"text", mode=0o644)
    assert entries["bytes.bin"].data == b"\x00\xff"
    assert entries["run"].mode == 0o755
    assert entries["tool.sh"].source_path == str(tool)
    assert Path(str(entries["tool.sh"].source_path)).read_text() == "after"
    assert entries["conf"].is_dir
    assert entries["conf/nested/a.txt"].source_path == str(
        tmp_path / "conf" / "nested" / "a.txt"
    )
    if _KEEPS_MODES:
        assert entries["tool.sh"].mode == 0o755


def test_fails_at_push_when_a_local_file_or_directory_is_gone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is not a file"):
        _base().add_local_file(str(tmp_path / "gone"), "/a")._zip_entries()
    with pytest.raises(ValueError, match="is not a directory"):
        _base().add_local_dir(str(tmp_path / "gone"), "/a")._zip_entries()


def test_adds_everything_in_a_local_directory_with_no_ignore_rules(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / ".git").mkdir(parents=True)
    (tmp_path / "src" / ".git" / "HEAD").write_text("ref")
    (tmp_path / "src" / ".env").write_text("SECRET=1")
    entries = _base().add_local_dir(str(tmp_path / "src"), "/app")._zip_entries()
    assert [entry.path for entry in entries] == [
        "Dockerfile",
        "src",
        "src/.env",
        "src/.git",
        "src/.git/HEAD",
    ]


def test_resolves_a_local_path_when_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("a")
    monkeypatch.chdir(tmp_path)
    builder = _base().add_local_file("a.txt", "/a.txt")
    monkeypatch.chdir(os.path.dirname(tmp_path))
    assert builder._zip_entries()[1].source_path == str(tmp_path / "a.txt")

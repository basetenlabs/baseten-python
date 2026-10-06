from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from baseten.sandbox import AsyncSandboxStream, SandboxError, SandboxStream
from baseten.sandbox._common import (
    format_duration,
    parse_duration,
    parse_timestamp,
    response_lines,
    response_lines_async,
)


def test_format_duration_rounds_to_milliseconds() -> None:
    assert format_duration(timedelta(hours=1)) == "3600000ms"
    assert format_duration(timedelta(microseconds=1500)) == "2ms"
    assert format_duration(timedelta(0)) == "0ms"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", timedelta(0)),
        ("90s", timedelta(seconds=90)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1.5h", timedelta(hours=1, minutes=30)),
        (".5s", timedelta(milliseconds=500)),
        ("3600000ms", timedelta(hours=1)),
        ("250us", timedelta(microseconds=250)),
        ("250µs", timedelta(microseconds=250)),
        ("1500ns", timedelta(microseconds=1.5)),
        ("+5m", timedelta(minutes=5)),
        ("-5m", timedelta(minutes=-5)),
        # Days and weeks only on their own, at 24 hours per day.
        ("7d", timedelta(days=7)),
        ("2w", timedelta(days=14)),
    ],
)
def test_parse_duration(value: str, expected: timedelta) -> None:
    assert parse_duration(value, "test duration") == expected


@pytest.mark.parametrize("value", ["", "5", "1d12h", "abc", "5m x", "1.5d"])
def test_parse_duration_rejects_invalid(value: str) -> None:
    with pytest.raises(SandboxError, match="test duration is not a valid duration"):
        parse_duration(value, "test duration")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # HTTP date, as process times come.
        (
            "Wed, 01 Jan 2023 12:00:00 GMT",
            datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC),
        ),
        # RFC 3339 with a space for the T, as health times come.
        (
            "2026-01-29 17:36:52+00:00",
            datetime(2026, 1, 29, 17, 36, 52, tzinfo=UTC),
        ),
        ("2026-10-06T12:00:00Z", datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)),
        (
            "2026-10-06T12:00:00+02:00",
            datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        ),
        (
            "2026-10-06T12:00:00.123456Z",
            datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=UTC),
        ),
        # Nanoseconds, as Go writes them, truncated to microseconds.
        (
            "2026-10-06T12:00:00.123456789Z",
            datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=UTC),
        ),
    ],
)
def test_parse_timestamp(value: str, expected: datetime) -> None:
    parsed = parse_timestamp(value, "test time")
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        # No zone, so the time would be a guess.
        "2026-10-06T12:00:00",
        "2026-10-06 12:00:00",
        "2026-10-06",
        # HTTP dates in any zone but GMT, or missing the weekday.
        "Wed, 01 Jan 2023 12:00:00 EST",
        "Wed, 01 Jan 2023 12:00:00 +0000",
        "01 Jan 2023 12:00:00 GMT",
        # Unix seconds.
        "1696593600",
    ],
)
def test_parse_timestamp_rejects_invalid(value: str) -> None:
    with pytest.raises(SandboxError, match="test time is not a valid timestamp"):
        parse_timestamp(value, "test time")


class _ChunkStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """Response body sent in the given chunks, recording whether it was closed."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((b"",), []),
        ((b"a\nb\n",), ["a", "b"]),
        # A last line without a line ending is still a line.
        ((b"a\nb",), ["a", "b"]),
        ((b"a\r\nb\r\n",), ["a", "b"]),
        # Lines split across chunks, including between \r and \n.
        ((b"ab", b"c\nd", b"e\n"), ["abc", "de"]),
        ((b"a\r", b"\nb"), ["a", "b"]),
        # A lone \r is part of the line, as a progress bar writes it.
        ((b"10%\r20%\n",), ["10%\r20%"]),
        ((b"a\n\nb\n",), ["a", "", "b"]),
        # A character split across chunks, and invalid UTF-8 replaced.
        (("é\n".encode()[:1], "é\n".encode()[1:]), ["é"]),
        ((b"a\xffb\n",), ["a�b"]),
    ],
)
class TestResponseLines:
    def test_sync(self, chunks: tuple[bytes, ...], expected: list[str]) -> None:
        response = httpx.Response(200, stream=_ChunkStream(*chunks))
        assert list(response_lines(response)) == expected

    @pytest.mark.asyncio
    async def test_async(self, chunks: tuple[bytes, ...], expected: list[str]) -> None:
        response = httpx.Response(200, stream=_ChunkStream(*chunks))
        assert [line async for line in response_lines_async(response)] == expected


def _upper(lines: Iterator[str]) -> Generator[str, None, None]:
    for line in lines:
        if line == "fail":
            raise ValueError("bad line")
        yield line.upper()


async def _upper_async(lines: AsyncIterator[str]) -> AsyncGenerator[str, None]:
    async for line in lines:
        if line == "fail":
            raise ValueError("bad line")
        yield line.upper()


class TestSandboxStream:
    def test_yields_items_and_closes_at_end(self) -> None:
        body = _ChunkStream(b"a\nb\n")
        stream = SandboxStream(httpx.Response(200, stream=body), _upper)
        assert list(stream) == ["A", "B"]
        assert body.closed

    def test_closes_on_error(self) -> None:
        body = _ChunkStream(b"a\nfail\nb\n")
        stream = SandboxStream(httpx.Response(200, stream=body), _upper)
        assert next(stream) == "A"
        with pytest.raises(ValueError, match="bad line"):
            next(stream)
        assert body.closed

    def test_closes_when_left_early(self) -> None:
        body = _ChunkStream(b"a\nb\n")
        with SandboxStream(httpx.Response(200, stream=body), _upper) as stream:
            assert next(stream) == "A"
        assert body.closed
        with pytest.raises(StopIteration):
            next(stream)

    def test_closes_before_first_item(self) -> None:
        body = _ChunkStream(b"a\n")
        SandboxStream(httpx.Response(200, stream=body), _upper).close()
        assert body.closed


class TestAsyncSandboxStream:
    @pytest.mark.asyncio
    async def test_yields_items_and_closes_at_end(self) -> None:
        body = _ChunkStream(b"a\nb\n")
        stream = AsyncSandboxStream(httpx.Response(200, stream=body), _upper_async)
        assert [item async for item in stream] == ["A", "B"]
        assert body.closed

    @pytest.mark.asyncio
    async def test_closes_on_error(self) -> None:
        body = _ChunkStream(b"a\nfail\nb\n")
        stream = AsyncSandboxStream(httpx.Response(200, stream=body), _upper_async)
        assert await anext(stream) == "A"
        with pytest.raises(ValueError, match="bad line"):
            await anext(stream)
        assert body.closed

    @pytest.mark.asyncio
    async def test_closes_when_left_early(self) -> None:
        body = _ChunkStream(b"a\nb\n")
        async with AsyncSandboxStream(
            httpx.Response(200, stream=body), _upper_async
        ) as stream:
            assert await anext(stream) == "A"
        assert body.closed
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_closes_before_first_item(self) -> None:
        body = _ChunkStream(b"a\n")
        await AsyncSandboxStream(httpx.Response(200, stream=body), _upper_async).close()
        assert body.closed

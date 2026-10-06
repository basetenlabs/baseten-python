from __future__ import annotations

import codecs
import email.utils
import re
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, Protocol, Self, TypeVar

import httpx

import baseten.client.sandboxapi
from baseten.sandbox._errors import SandboxError, converted_errors
from baseten.sandbox._retry import (
    ResolvedRetryOptions,
    retry_idempotent,
    retry_idempotent_async,
)

_T = TypeVar("_T")
_ItemT = TypeVar("_ItemT")
_ItemT_co = TypeVar("_ItemT_co", covariant=True)

_LINE_BREAK = re.compile(r"\r?\n")

_HTTP_DATE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
)

_DURATION_UNIT_MICROSECONDS = {
    "ns": 0.001,
    "us": 1.0,
    "µs": 1.0,
    "μs": 1.0,
    "ms": 1_000.0,
    "s": 1_000_000.0,
    "m": 60_000_000.0,
    "h": 3_600_000_000.0,
}

_WHOLE_DAYS_OR_WEEKS = re.compile(r"([0-9]+)([dw])")

_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)")


def format_duration(value: timedelta) -> str:
    """Format a control plane duration, rounded to whole milliseconds."""
    return f"{round(value / timedelta(milliseconds=1))}ms"


def parse_duration(value: str, what: str) -> timedelta:
    """Parse a control plane duration, naming it as ``what`` in errors."""
    # Go duration syntax, or a whole number of days or weeks on its own, at
    # 24 hours per day.
    sign = -1 if value.startswith("-") else 1
    body = value[1:] if value.startswith(("-", "+")) else value
    if body == "0":
        return timedelta(0)
    whole = _WHOLE_DAYS_OR_WEEKS.fullmatch(body)
    if whole is not None:
        days = int(whole[1]) * (7 if whole[2] == "w" else 1)
        return sign * timedelta(days=days)
    total_microseconds = 0.0
    index = 0
    while index < len(body):
        part = _DURATION_PART.match(body, index)
        if part is None:
            break
        total_microseconds += float(part[1]) * _DURATION_UNIT_MICROSECONDS[part[2]]
        index = part.end()
    if body == "" or index < len(body):
        raise SandboxError(f"{what} is not a valid duration: {value!r}")
    return sign * timedelta(microseconds=total_microseconds)


def parse_timestamp(value: str, what: str) -> datetime:
    """Parse an execution API timestamp, naming it as ``what`` in errors."""
    # The API declares no format, and its examples differ: HTTP dates, RFC
    # 3339, and RFC 3339 with a space for the T. A time without a zone is
    # rejected rather than guessed at.
    if _HTTP_DATE.fullmatch(value):
        return email.utils.parsedate_to_datetime(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise SandboxError(f"{what} is not a valid timestamp: {value!r}")
    return parsed


@dataclass(kw_only=True)
class SandboxContext:
    """What a :class:`Sandbox` and its subsystems send requests with."""

    name: str
    url: str
    http_client: httpx.Client
    auth: httpx.Auth
    headers: Mapping[str, str]
    raw_api: baseten.client.sandboxapi.ApiClient
    """The sandbox's execution API, authenticated."""
    retry: ResolvedRetryOptions
    executor: ThreadPoolExecutor | None
    """The caller's thread pool for upload parts, if given."""

    @contextmanager
    def api(self) -> Iterator[baseten.client.sandboxapi.ApiClient]:
        """Give the generated client, raising its error responses as ours."""
        with converted_errors("exec"):
            yield self.raw_api

    @contextmanager
    def api_with_timeout(
        self, timeout: timedelta
    ) -> Iterator[baseten.client.sandboxapi.ApiClient]:
        """As :meth:`api`, with requests timing out after ``timeout``."""
        with converted_errors("exec"):
            yield baseten.client.sandboxapi.ApiClient(
                self.http_client,
                base_url=self.url,
                auth=self.auth,
                headers=self.headers,
                timeout=httpx.Timeout(timeout.total_seconds()),
            )

    def call_read(
        self, call: Callable[[baseten.client.sandboxapi.ApiClient], _T]
    ) -> _T:
        """Run an idempotent call, retrying it on the read budgets."""

        def attempt() -> _T:
            with self.api() as api:
                return call(api)

        return retry_idempotent(
            attempt,
            max_retries=self.retry.read_max_retries,
            gateway_max_retries=self.retry.gateway_max_retries,
        )

    def call_upload(
        self, call: Callable[[baseten.client.sandboxapi.ApiClient], _T]
    ) -> _T:
        """Run an idempotent upload, retrying it on the upload budgets."""

        def attempt() -> _T:
            with self.api() as api:
                return call(api)

        return retry_idempotent(
            attempt,
            max_retries=self.retry.upload_max_retries,
            gateway_max_retries=self.retry.gateway_max_retries,
        )

    def send(self, request: httpx.Request) -> None:
        """Send a request the generated client cannot build, raising an error response as ours."""
        with converted_errors("exec"):
            response = self.http_client.send(request, auth=self.auth)
            if not response.is_success:
                raise baseten.client.sandboxapi.ResponseError(
                    status_code=response.status_code, body=response.text
                )

    def build_request(self, method: str, path: str, **kwargs: Any) -> httpx.Request:
        """Build a request to a path of the sandbox, with the client's headers."""
        return self.http_client.build_request(
            method, self.url.rstrip("/") + path, headers=self.headers, **kwargs
        )


@dataclass(kw_only=True)
class AsyncSandboxContext:
    """Async form of :class:`SandboxContext`."""

    name: str
    url: str
    http_client: httpx.AsyncClient
    auth: httpx.Auth
    headers: Mapping[str, str]
    raw_api: baseten.client.sandboxapi.AsyncApiClient
    """The sandbox's execution API, authenticated."""
    retry: ResolvedRetryOptions

    @contextmanager
    def api(self) -> Iterator[baseten.client.sandboxapi.AsyncApiClient]:
        """Give the generated client, raising its error responses as ours."""
        # A plain context manager suffices, since it only wraps awaited calls.
        with converted_errors("exec"):
            yield self.raw_api

    @contextmanager
    def api_with_timeout(
        self, timeout: timedelta
    ) -> Iterator[baseten.client.sandboxapi.AsyncApiClient]:
        """As :meth:`api`, with requests timing out after ``timeout``."""
        with converted_errors("exec"):
            yield baseten.client.sandboxapi.AsyncApiClient(
                self.http_client,
                base_url=self.url,
                auth=self.auth,
                headers=self.headers,
                timeout=httpx.Timeout(timeout.total_seconds()),
            )

    async def call_read(
        self, call: Callable[[baseten.client.sandboxapi.AsyncApiClient], Awaitable[_T]]
    ) -> _T:
        """Run an idempotent call, retrying it on the read budgets."""

        async def attempt() -> _T:
            with self.api() as api:
                return await call(api)

        return await retry_idempotent_async(
            attempt,
            max_retries=self.retry.read_max_retries,
            gateway_max_retries=self.retry.gateway_max_retries,
        )

    async def call_upload(
        self, call: Callable[[baseten.client.sandboxapi.AsyncApiClient], Awaitable[_T]]
    ) -> _T:
        """Run an idempotent upload, retrying it on the upload budgets."""

        async def attempt() -> _T:
            with self.api() as api:
                return await call(api)

        return await retry_idempotent_async(
            attempt,
            max_retries=self.retry.upload_max_retries,
            gateway_max_retries=self.retry.gateway_max_retries,
        )

    async def send(self, request: httpx.Request) -> None:
        """Send a request the generated client cannot build, raising an error response as ours."""
        with converted_errors("exec"):
            response = await self.http_client.send(request, auth=self.auth)
            if not response.is_success:
                raise baseten.client.sandboxapi.ResponseError(
                    status_code=response.status_code, body=response.text
                )

    def build_request(self, method: str, path: str, **kwargs: Any) -> httpx.Request:
        """Build a request to a path of the sandbox, with the client's headers."""
        return self.http_client.build_request(
            method, self.url.rstrip("/") + path, headers=self.headers, **kwargs
        )


class SandboxStream(Generic[_ItemT]):
    """Items a sandbox sends as they happen, read by iterating.

    Reading to the end, or an error while reading, closes the stream. To stop
    early, use it as a context manager or call :meth:`close`. Closing stops
    only the stream, never what it reports on, such as a running process.
    """

    def __init__(
        self,
        response: httpx.Response,
        parse: Callable[[Iterator[str]], Generator[_ItemT, None, None]],
    ) -> None:
        """Internal. Returned by the methods that stream.

        :meta private:
        """
        self._response = response
        self._items = parse(response_lines(response))

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> _ItemT:
        try:
            return next(self._items)
        except BaseException:
            # Includes the StopIteration at the end.
            self.close()
            raise

    def close(self) -> None:
        """Stop reading and release the connection. Does nothing if already closed."""
        self._items.close()
        # A generator closed before it started never runs its own cleanup,
        # so the response is closed here rather than by the generator.
        self._response.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncSandboxStream(Generic[_ItemT]):
    """Async form of :class:`SandboxStream`."""

    def __init__(
        self,
        response: httpx.Response,
        parse: Callable[[AsyncIterator[str]], AsyncGenerator[_ItemT, None]],
    ) -> None:
        """Internal. Returned by the methods that stream.

        :meta private:
        """
        self._response = response
        self._items = parse(response_lines_async(response))

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> _ItemT:
        try:
            return await anext(self._items)
        except BaseException:
            # Includes the StopAsyncIteration at the end.
            await self.close()
            raise

    async def close(self) -> None:
        """Stop reading and release the connection. Does nothing if already closed."""
        await self._items.aclose()
        await self._response.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class _Pagination(Protocol):
    """Pagination fields of a control plane listing page."""

    @property
    def has_more(self) -> bool: ...

    @property
    def cursor(self) -> str | None: ...


class _Page(Protocol[_ItemT_co]):
    """A control plane listing page."""

    @property
    def items(self) -> Sequence[_ItemT_co]: ...

    @property
    def pagination(self) -> _Pagination: ...


def paginate(
    what: str, fetch_page: Callable[[str | None], _Page[_ItemT_co]]
) -> Iterator[_ItemT_co]:
    """Yield every item of a cursor-paginated control plane listing.

    Each page is fetched as iteration reaches it. ``what`` names the listing
    in errors.
    """
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        page = fetch_page(cursor)
        yield from page.items
        cursor = _next_cursor(what, page, seen_cursors)
        if cursor is None:
            return


async def paginate_async(
    what: str, fetch_page: Callable[[str | None], Awaitable[_Page[_ItemT_co]]]
) -> AsyncIterator[_ItemT_co]:
    """Async form of :func:`paginate`."""
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        page = await fetch_page(cursor)
        for item in page.items:
            yield item
        cursor = _next_cursor(what, page, seen_cursors)
        if cursor is None:
            return


def _next_cursor(what: str, page: _Page[Any], seen_cursors: set[str]) -> str | None:
    """Return the cursor of the page after ``page``, or ``None`` after the last."""
    cursor = page.pagination.cursor if page.pagination.has_more else None
    # A server repeating a cursor would otherwise page forever.
    if cursor is not None:
        if cursor in seen_cursors:
            raise SandboxError(f"{what} returned a repeated cursor")
        seen_cursors.add(cursor)
    return cursor


def response_lines(response: httpx.Response) -> Generator[str, None, None]:
    """Yield a response body's lines without line endings, including a last one with none."""
    # Only \n and \r\n end a line, unlike httpx's iter_lines, which also
    # splits at a lone \r, such as a progress bar's.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    for chunk in response.iter_bytes():
        buffer += decoder.decode(chunk)
        *lines, buffer = _LINE_BREAK.split(buffer)
        yield from lines
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer


async def response_lines_async(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Async form of :func:`response_lines`."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    async for chunk in response.aiter_bytes():
        buffer += decoder.decode(chunk)
        *lines, buffer = _LINE_BREAK.split(buffer)
        for line in lines:
            yield line
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer

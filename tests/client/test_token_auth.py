from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from baseten.client import AccessToken
from baseten.client._token_auth import (
    AsyncTokenCache,
    TokenCache,
    _applies_to,
    _cached,
    is_token_refused,
)

# How long each mint in the concurrency tests takes, long enough for every
# caller to be waiting on the first one.
_SLOW_MINT_SEC = 0.2


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _Mints:
    """Issues ``token-<n>`` valid for *lifetime* from the clock's time."""

    def __init__(self, clock: _Clock, lifetime: timedelta) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.count = 0
        self.failures = 0

    def mint(self) -> AccessToken:
        self.count += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("mint failed")
        return AccessToken(
            token=f"token-{self.count}", expires_at=self.clock() + self.lifetime
        )

    async def mint_async(self) -> AccessToken:
        return self.mint()


def _cache(
    lifetime: timedelta, refresh_leeway: timedelta
) -> tuple[TokenCache, _Clock, _Mints]:
    clock = _Clock()
    mints = _Mints(clock, lifetime)
    cache = TokenCache(mint=mints.mint, refresh_leeway=refresh_leeway, now=clock)
    return cache, clock, mints


class TestTokenCache:
    def test_refreshes_before_expiry(self) -> None:
        cache, clock, mints = _cache(timedelta(hours=1), timedelta(minutes=5))
        assert cache.token().token == "token-1"
        clock.now += timedelta(minutes=54, seconds=59)
        assert cache.token().token == "token-1"
        clock.now += timedelta(seconds=1)
        assert cache.token().token == "token-2"
        assert mints.count == 2

    def test_refresh_capped_at_half_lifetime(self) -> None:
        # Without the cap, a five-minute margin on a one-minute token would
        # mint on every call.
        cache, clock, mints = _cache(timedelta(minutes=1), timedelta(minutes=5))
        cache.token()
        clock.now += timedelta(seconds=29)
        cache.token()
        assert mints.count == 1
        clock.now += timedelta(seconds=1)
        cache.token()
        assert mints.count == 2

    def test_returns_just_minted_token_even_if_due(self) -> None:
        cache, _, mints = _cache(timedelta(0), timedelta(minutes=1))
        assert cache.token().token == "token-1"
        assert cache.token().token == "token-2"
        assert mints.count == 2

    def test_invalidate_keeps_newer_token(self) -> None:
        cache, _, _ = _cache(timedelta(hours=1), timedelta(minutes=1))
        cache.token()
        cache.invalidate("token-0")
        assert cache.token().token == "token-1"
        cache.invalidate("token-1")
        assert cache.token().token == "token-2"

    def test_failed_mint_is_not_cached(self) -> None:
        cache, _, mints = _cache(timedelta(hours=1), timedelta(minutes=1))
        mints.failures = 1
        with pytest.raises(RuntimeError):
            cache.token()
        assert cache.token().token == "token-2"

    def test_waiter_on_failed_mint_tries_its_own(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(hours=1))
        mints.failures = 1
        release = threading.Event()
        first_minting = threading.Event()

        def mint() -> AccessToken:
            first_minting.set()
            release.wait()
            return mints.mint()

        cache = TokenCache(mint=mint, refresh_leeway=timedelta(minutes=1), now=clock)
        errors: list[BaseException] = []
        results: list[str] = []

        def first() -> None:
            try:
                cache.token()
            except RuntimeError as e:
                errors.append(e)

        first_thread = threading.Thread(target=first)
        first_thread.start()
        first_minting.wait()
        waiter = threading.Thread(target=lambda: results.append(cache.token().token))
        waiter.start()
        release.set()
        first_thread.join()
        waiter.join()
        assert len(errors) == 1
        assert results == ["token-2"]

    def test_concurrent_callers_on_failing_mint_share_one_retry(self) -> None:
        calls = 0

        def mint() -> AccessToken:
            nonlocal calls
            calls += 1
            time.sleep(_SLOW_MINT_SEC)
            raise RuntimeError("mint failed")

        cache = TokenCache(mint=mint, refresh_leeway=timedelta(minutes=1))
        errors: list[BaseException] = []

        def call() -> None:
            try:
                cache.token()
            except RuntimeError as e:
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(8)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started
        assert len(errors) == 8
        # One failed mint, then one retry shared by everyone who joined it,
        # rather than one mint per caller in turn.
        assert calls == 2
        assert elapsed < 3 * _SLOW_MINT_SEC

    def test_failed_refresh_falls_back_to_previous_token(self) -> None:
        cache, clock, mints = _cache(timedelta(hours=1), timedelta(minutes=5))
        assert cache.token().token == "token-1"
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert cache.token().token == "token-1"
        assert cache.token().token == "token-3"
        # The successful mint dropped the fallback.
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert cache.token().token == "token-3"

    def test_no_fallback_once_previous_expired(self) -> None:
        cache, clock, mints = _cache(timedelta(hours=1), timedelta(minutes=5))
        cache.token()
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert cache.token().token == "token-1"
        clock.now += timedelta(minutes=4)
        mints.failures = 1
        with pytest.raises(RuntimeError):
            cache.token()

    def test_invalidate_clears_previous_token(self) -> None:
        cache, clock, mints = _cache(timedelta(hours=1), timedelta(minutes=5))
        cache.token()
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert cache.token().token == "token-1"
        cache.invalidate("token-1")
        mints.failures = 1
        with pytest.raises(RuntimeError):
            cache.token()


class TestAsyncTokenCache:
    @pytest.mark.asyncio
    async def test_refreshes_and_caps(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(minutes=1))
        cache = AsyncTokenCache(
            mint=mints.mint_async, refresh_leeway=timedelta(minutes=5), now=clock
        )
        assert (await cache.token()).token == "token-1"
        clock.now += timedelta(seconds=29)
        assert (await cache.token()).token == "token-1"
        clock.now += timedelta(seconds=1)
        assert (await cache.token()).token == "token-2"

    @pytest.mark.asyncio
    async def test_shares_one_mint_and_waiter_retries_after_failure(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(hours=1))
        release = asyncio.Event()

        async def mint() -> AccessToken:
            await release.wait()
            return mints.mint()

        cache = AsyncTokenCache(
            mint=mint, refresh_leeway=timedelta(minutes=1), now=clock
        )
        mints.failures = 1
        first = asyncio.create_task(cache.token())
        waiters = [asyncio.create_task(cache.token()) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError):
            await first
        assert [(await w).token for w in waiters] == ["token-2"] * 3
        assert mints.count == 2

    @pytest.mark.asyncio
    async def test_invalidate_keeps_newer_token(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(hours=1))
        cache = AsyncTokenCache(
            mint=mints.mint_async, refresh_leeway=timedelta(minutes=1), now=clock
        )
        await cache.token()
        cache.invalidate("token-0")
        assert (await cache.token()).token == "token-1"
        cache.invalidate("token-1")
        assert (await cache.token()).token == "token-2"

    @pytest.mark.asyncio
    async def test_concurrent_callers_on_failing_mint_share_one_retry(self) -> None:
        calls = 0

        async def mint() -> AccessToken:
            nonlocal calls
            calls += 1
            await asyncio.sleep(_SLOW_MINT_SEC)
            raise RuntimeError("mint failed")

        cache = AsyncTokenCache(mint=mint, refresh_leeway=timedelta(minutes=1))
        started = time.monotonic()
        results = await asyncio.gather(
            *(cache.token() for _ in range(8)), return_exceptions=True
        )
        elapsed = time.monotonic() - started
        assert all(isinstance(result, RuntimeError) for result in results)
        assert calls == 2
        assert elapsed < 3 * _SLOW_MINT_SEC

    @pytest.mark.asyncio
    async def test_failed_refresh_falls_back_to_previous_token(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(hours=1))
        cache = AsyncTokenCache(
            mint=mints.mint_async, refresh_leeway=timedelta(minutes=5), now=clock
        )
        await cache.token()
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert (await cache.token()).token == "token-1"
        cache.invalidate("token-1")
        mints.failures = 1
        with pytest.raises(RuntimeError):
            await cache.token()
        assert (await cache.token()).token == "token-4"
        clock.now += timedelta(minutes=56)
        mints.failures = 1
        assert (await cache.token()).token == "token-4"
        clock.now += timedelta(minutes=4)
        mints.failures = 1
        with pytest.raises(RuntimeError):
            await cache.token()


@pytest.mark.parametrize(
    ("url", "applies"),
    [
        ("https://api.test/x", True),
        ("http://api.test/x", False),
        ("https://api.test:8443/x", False),
        ("https://other.test/x", False),
    ],
)
def test_applies_only_to_origin(url: str, applies: bool) -> None:
    origin = httpx.URL("https://api.test")
    assert _applies_to(httpx.Request("GET", url), origin) is applies


def test_access_token_repr_hides_token() -> None:
    token = AccessToken(
        token="secret-bearer", expires_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert "secret-bearer" not in repr(token)
    cached = _cached(token, minted_at=token.expires_at, refresh_leeway=timedelta(0))
    assert "secret-bearer" not in repr(cached)


@pytest.mark.parametrize(
    ("status", "headers", "refused"),
    [
        (401, {"x-blaxel-error-code": "TOKEN_REVOKED"}, True),
        (401, {"WWW-Authenticate": 'Bearer error="invalid_token"'}, False),
        (401, {}, False),
        (401, {"WWW-Authenticate": 'Bearer realm="baseten"'}, False),
        (403, {"x-blaxel-error-code": "TOKEN_REVOKED"}, False),
    ],
)
def test_is_token_refused(status: int, headers: dict[str, str], refused: bool) -> None:
    assert is_token_refused(httpx.Response(status, headers=headers)) is refused

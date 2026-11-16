from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from baseten.client import AccessToken
from baseten.client._token_auth import (
    AsyncTokenCache,
    TokenCache,
    is_token_refused,
)


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
    lifetime: timedelta, refresh_before: timedelta
) -> tuple[TokenCache, _Clock, _Mints]:
    clock = _Clock()
    mints = _Mints(clock, lifetime)
    cache = TokenCache(mint=mints.mint, refresh_before=refresh_before, now=clock)
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

        cache = TokenCache(mint=mint, refresh_before=timedelta(minutes=1), now=clock)
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


class TestAsyncTokenCache:
    @pytest.mark.asyncio
    async def test_refreshes_and_caps(self) -> None:
        clock = _Clock()
        mints = _Mints(clock, timedelta(minutes=1))
        cache = AsyncTokenCache(
            mint=mints.mint_async, refresh_before=timedelta(minutes=5), now=clock
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
            mint=mint, refresh_before=timedelta(minutes=1), now=clock
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
            mint=mints.mint_async, refresh_before=timedelta(minutes=1), now=clock
        )
        await cache.token()
        cache.invalidate("token-0")
        assert (await cache.token()).token == "token-1"
        cache.invalidate("token-1")
        assert (await cache.token()).token == "token-2"


@pytest.mark.parametrize(
    ("status", "headers", "refused"),
    [
        (401, {"x-blaxel-error-code": "TOKEN_REVOKED"}, True),
        (
            401,
            {"WWW-Authenticate": 'Bearer realm="baseten", error="invalid_token"'},
            True,
        ),
        (401, {}, False),
        (401, {"WWW-Authenticate": 'Bearer realm="baseten"'}, False),
        (403, {"WWW-Authenticate": 'Bearer error="invalid_token"'}, False),
    ],
)
def test_is_token_refused(status: int, headers: dict[str, str], refused: bool) -> None:
    assert is_token_refused(httpx.Response(status, headers=headers)) is refused

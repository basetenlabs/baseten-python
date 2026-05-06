from __future__ import annotations

import re

from baseten.client._user_agent import user_agent_header, with_user_agent


def test_user_agent_header_format() -> None:
    assert re.match(r"^baseten-python/\S+ \(Python/\S+; \S+\)$", user_agent_header())


def test_with_user_agent_sets_when_absent() -> None:
    out = with_user_agent({})
    assert out["User-Agent"] == user_agent_header()


def test_with_user_agent_does_not_overwrite_any_case() -> None:
    out = with_user_agent({"user-agent": "custom/1.0"})
    assert out["user-agent"] == "custom/1.0"
    assert "User-Agent" not in out


def test_with_user_agent_returns_copy() -> None:
    src: dict[str, str] = {}
    out = with_user_agent(src)
    assert src == {}
    assert "User-Agent" in out

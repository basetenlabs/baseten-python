import platform
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version


def _package_version() -> str:
    try:
        return version("baseten")
    except PackageNotFoundError:
        return "dev"


def user_agent_header() -> str:
    """Build a User-Agent value like ``baseten-python/0.9.0 (Python/3.13.2; Linux)``."""
    return (
        f"baseten-python/{_package_version()} "
        f"(Python/{platform.python_version()}; {platform.system()})"
    )


def with_user_agent(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with our User-Agent set, unless one is already present."""
    if any(key.lower() == "user-agent" for key in headers):
        return dict(headers)
    return {**headers, "User-Agent": user_agent_header()}

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

import baseten.client.managementapi
from baseten.sandbox._common import (
    AsyncSandboxContext,
    SandboxContext,
    format_duration,
    paginate,
    paginate_async,
    parse_duration,
)
from baseten.sandbox._info import set_fields

SandboxPreviewStatus: TypeAlias = (
    Literal["DEPLOYING", "DEPLOYED", "FAILED", "DELETING", "TERMINATED"] | str
)
"""Deployment status of a preview.

Other values may be added, so do not treat this list as exhaustive.
"""


@dataclass(kw_only=True)
class SandboxPreviewInfo:
    """A preview as last reported by the control plane."""

    name: str
    """Name of the preview, unique within the sandbox."""

    port: int
    """Sandbox port the preview exposes."""

    url: str
    """HTTPS URL serving the port.

    Requests to a private preview need a token from
    :meth:`SandboxPreviews.create_token`.
    """

    status: SandboxPreviewStatus

    public: bool
    """Whether the URL is reachable without a token."""

    response_headers: dict[str, str]
    """Headers added to every response the preview serves."""

    request_headers: dict[str, str]
    """Headers added to every request forwarded to the sandbox.

    Values come back as ``"****"`` unless read with *show_secrets*.
    """

    ttl: timedelta | None
    """Lifetime set by the last create or update that had one, counted from that request.

    :attr:`expires_at` is when the preview is deleted.
    """

    expires_at: datetime | None
    """When the preview is deleted, when it expires."""

    region: str | None
    """Region serving the preview, the sandbox's region."""

    created_at: datetime | None

    updated_at: datetime | None

    created_by: str | None

    updated_by: str | None


@dataclass(kw_only=True)
class SandboxPreviewTokenInfo:
    """An access token for a private preview."""

    name: str
    """Name of the token."""

    token: str
    """Value granting access.

    Append it to the preview URL as ``?preview_token=<token>``; the first
    request sets a cookie so a browser can keep navigating without it.
    """

    expires_at: datetime
    """When the token stops working."""

    expired: bool
    """Whether :attr:`expires_at` has passed."""


class SandboxPreviews:
    """A sandbox's previews, which expose its ports on HTTPS URLs.

    Obtained from :attr:`Sandbox.previews`. Previews need the sandbox's name,
    so every call fails on a sandbox from :meth:`SandboxClient.sandbox_from_url`.
    """

    def __init__(self, context: SandboxContext) -> None:
        """Internal. Use :attr:`Sandbox.previews` instead.

        :meta private:
        """
        self._context = context

    def create(
        self,
        *,
        port: int,
        name: str | None = None,
        create_if_not_exists: bool | None = None,
        public: bool | None = None,
        response_headers: Mapping[str, str] | None = None,
        request_headers: Mapping[str, str] | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> SandboxPreviewInfo:
        """Create a preview exposing a sandbox port.

        Args:
            port: Sandbox port to expose.
            name: Name of the preview, unique within the sandbox. Assigned by
                the server when ``None``.
            create_if_not_exists: When true, return the existing preview with
                this name, unchanged, instead of failing.
            public: Make the URL reachable without a token. Defaults to false,
                a private preview.
            response_headers: Headers added to every response the preview
                serves, such as CORS headers.
            request_headers: Headers added to every request forwarded to the
                sandbox, such as an Authorization header the application
                expects.
            ttl: Delete the preview this long after the request. Cannot be
                combined with *expires_at*.
            expires_at: Delete the preview at this time. Cannot be combined
                with *ttl*.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = api.create_sandbox_preview(
                sandbox_name=sandbox_name,
                params=baseten.client.managementapi.CreateSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxPreviewRequest(
                    port=port,
                    **set_fields(
                        name=name,
                        create_if_not_exists=create_if_not_exists,
                        public=public,
                        response_headers=_optional_dict(response_headers),
                        request_headers=_optional_dict(request_headers),
                        ttl=None if ttl is None else format_duration(ttl),
                        expires_at=expires_at,
                    ),
                ),
            )
        return _preview_info_from_api(preview)

    def get_info(
        self, *, name: str, show_secrets: bool | None = None
    ) -> SandboxPreviewInfo:
        """Get a preview's current record.

        Args:
            name: Name of the preview.
            show_secrets: Reveal request header values for workspace
                administrators. Defaults to false. Callers without the admin
                role receive masked values even when true.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = api.get_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.GetSandboxPreviewParams(
                    **set_fields(
                        team_id=self._context.team_id, show_secrets=show_secrets
                    )
                ),
            )
        return _preview_info_from_api(preview)

    def list(self, *, page_size: int | None = None) -> Iterator[SandboxPreviewInfo]:
        """List the sandbox's previews, fetching further pages as iteration reaches them.

        Args:
            page_size: How many previews to fetch per underlying request.
        """
        sandbox_name = _sandbox_name(self._context.name)

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxPreviewsResponse:
            with self._context.control_api() as api:
                return api.list_sandbox_previews(
                    sandbox_name=sandbox_name,
                    params=baseten.client.managementapi.ListSandboxPreviewsParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                        )
                    ),
                )

        for preview in paginate("preview list", fetch_page):
            yield _preview_info_from_api(preview)

    def update(
        self,
        *,
        name: str,
        public: bool | None = None,
        response_headers: Mapping[str, str] | None = None,
        request_headers: Mapping[str, str] | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> SandboxPreviewInfo:
        """Update a preview, returning its record after the change.

        Arguments left ``None`` stay unchanged. The URL does not change, and
        the name and port cannot be changed.

        Args:
            name: Name of the preview.
            public: Whether the URL is reachable without a token.
            response_headers: Headers added to every response, replacing the
                whole set: headers left out are removed, and an empty mapping
                removes them all.
            request_headers: Headers added to every forwarded request,
                replacing the whole set: headers left out are removed, and an
                empty mapping removes them all. Sending back a ``"****"`` value
                from :attr:`SandboxPreviewInfo.request_headers` is rejected, so
                to change one header, get the others with *show_secrets* and
                send them all.
            ttl: Delete the preview this long after the request, replacing any
                previous expiration. Cannot be combined with *expires_at*.
            expires_at: Delete the preview at this time, replacing any previous
                expiration. Cannot be combined with *ttl*.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = api.update_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.UpdateSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.UpdateSandboxPreviewRequest(
                    **set_fields(
                        public=public,
                        response_headers=_optional_dict(response_headers),
                        request_headers=_optional_dict(request_headers),
                        ttl=None if ttl is None else format_duration(ttl),
                        expires_at=expires_at,
                    )
                ),
            )
        return _preview_info_from_api(preview)

    def delete(self, *, name: str) -> SandboxPreviewInfo:
        """Delete a preview and its tokens, returning its record.

        The URL stops serving at once, but the record may show the preview as
        deleting while the server finishes; this does not wait for that.

        Args:
            name: Name of the preview.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = api.delete_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.DeleteSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _preview_info_from_api(preview)

    def create_token(
        self,
        *,
        preview_name: str,
        expires_at: datetime,
        create_if_not_exists: bool | None = None,
    ) -> SandboxPreviewTokenInfo:
        """Create a token granting access to a private preview.

        Args:
            preview_name: Name of the private preview.
            expires_at: When the token stops working.
            create_if_not_exists: When true, return an existing unexpired token
                of the preview that stays valid at least until *expires_at*,
                instead of creating one. A token created moments before may not
                be found yet, so a new one may be created anyway.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            token = api.create_sandbox_preview_token(
                sandbox_name=sandbox_name,
                preview_name=preview_name,
                params=baseten.client.managementapi.CreateSandboxPreviewTokenParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxPreviewTokenRequest(
                    expires_at=expires_at,
                    **set_fields(create_if_not_exists=create_if_not_exists),
                ),
            )
        return _preview_token_info_from_api(token)

    def list_tokens(
        self, *, preview_name: str, page_size: int | None = None
    ) -> Iterator[SandboxPreviewTokenInfo]:
        """List a private preview's tokens, fetching further pages as iteration reaches them.

        Expired tokens are included until the server removes them. A public
        preview has no tokens, and the server rejects the call.

        Args:
            preview_name: Name of the private preview.
            page_size: How many tokens to fetch per underlying request.
        """
        sandbox_name = _sandbox_name(self._context.name)

        def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxPreviewTokensResponse:
            with self._context.control_api() as api:
                return api.list_sandbox_preview_tokens(
                    sandbox_name=sandbox_name,
                    preview_name=preview_name,
                    params=baseten.client.managementapi.ListSandboxPreviewTokensParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                        )
                    ),
                )

        for token in paginate("preview token list", fetch_page):
            yield _preview_token_info_from_api(token)

    def delete_token(self, *, preview_name: str, token_name: str) -> None:
        """Revoke a preview token. Requests using it are rejected at once.

        Args:
            preview_name: Name of the private preview.
            token_name: Name of the token.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            api.delete_sandbox_preview_token(
                sandbox_name=sandbox_name,
                preview_name=preview_name,
                token_name=token_name,
                params=baseten.client.managementapi.DeleteSandboxPreviewTokenParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )


class AsyncSandboxPreviews:
    """A sandbox's previews, which expose its ports on HTTPS URLs.

    Obtained from :attr:`AsyncSandbox.previews`. Previews need the sandbox's
    name, so every call fails on a sandbox from
    :meth:`AsyncSandboxClient.sandbox_from_url`.
    """

    def __init__(self, context: AsyncSandboxContext) -> None:
        """Internal. Use :attr:`AsyncSandbox.previews` instead.

        :meta private:
        """
        self._context = context

    async def create(
        self,
        *,
        port: int,
        name: str | None = None,
        create_if_not_exists: bool | None = None,
        public: bool | None = None,
        response_headers: Mapping[str, str] | None = None,
        request_headers: Mapping[str, str] | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> SandboxPreviewInfo:
        """Create a preview exposing a sandbox port.

        Args:
            port: Sandbox port to expose.
            name: Name of the preview, unique within the sandbox. Assigned by
                the server when ``None``.
            create_if_not_exists: When true, return the existing preview with
                this name, unchanged, instead of failing.
            public: Make the URL reachable without a token. Defaults to false,
                a private preview.
            response_headers: Headers added to every response the preview
                serves, such as CORS headers.
            request_headers: Headers added to every request forwarded to the
                sandbox, such as an Authorization header the application
                expects.
            ttl: Delete the preview this long after the request. Cannot be
                combined with *expires_at*.
            expires_at: Delete the preview at this time. Cannot be combined
                with *ttl*.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = await api.create_sandbox_preview(
                sandbox_name=sandbox_name,
                params=baseten.client.managementapi.CreateSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxPreviewRequest(
                    port=port,
                    **set_fields(
                        name=name,
                        create_if_not_exists=create_if_not_exists,
                        public=public,
                        response_headers=_optional_dict(response_headers),
                        request_headers=_optional_dict(request_headers),
                        ttl=None if ttl is None else format_duration(ttl),
                        expires_at=expires_at,
                    ),
                ),
            )
        return _preview_info_from_api(preview)

    async def get_info(
        self, *, name: str, show_secrets: bool | None = None
    ) -> SandboxPreviewInfo:
        """Get a preview's current record.

        Args:
            name: Name of the preview.
            show_secrets: Reveal request header values for workspace
                administrators. Defaults to false. Callers without the admin
                role receive masked values even when true.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = await api.get_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.GetSandboxPreviewParams(
                    **set_fields(
                        team_id=self._context.team_id, show_secrets=show_secrets
                    )
                ),
            )
        return _preview_info_from_api(preview)

    async def list(
        self, *, page_size: int | None = None
    ) -> AsyncIterator[SandboxPreviewInfo]:
        """List the sandbox's previews, fetching further pages as iteration reaches them.

        Args:
            page_size: How many previews to fetch per underlying request.
        """
        sandbox_name = _sandbox_name(self._context.name)

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxPreviewsResponse:
            with self._context.control_api() as api:
                return await api.list_sandbox_previews(
                    sandbox_name=sandbox_name,
                    params=baseten.client.managementapi.ListSandboxPreviewsParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                        )
                    ),
                )

        async for preview in paginate_async("preview list", fetch_page):
            yield _preview_info_from_api(preview)

    async def update(
        self,
        *,
        name: str,
        public: bool | None = None,
        response_headers: Mapping[str, str] | None = None,
        request_headers: Mapping[str, str] | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> SandboxPreviewInfo:
        """Update a preview, returning its record after the change.

        Arguments left ``None`` stay unchanged. The URL does not change, and
        the name and port cannot be changed.

        Args:
            name: Name of the preview.
            public: Whether the URL is reachable without a token.
            response_headers: Headers added to every response, replacing the
                whole set: headers left out are removed, and an empty mapping
                removes them all.
            request_headers: Headers added to every forwarded request,
                replacing the whole set: headers left out are removed, and an
                empty mapping removes them all. Sending back a ``"****"`` value
                from :attr:`SandboxPreviewInfo.request_headers` is rejected, so
                to change one header, get the others with *show_secrets* and
                send them all.
            ttl: Delete the preview this long after the request, replacing any
                previous expiration. Cannot be combined with *expires_at*.
            expires_at: Delete the preview at this time, replacing any previous
                expiration. Cannot be combined with *ttl*.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = await api.update_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.UpdateSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.UpdateSandboxPreviewRequest(
                    **set_fields(
                        public=public,
                        response_headers=_optional_dict(response_headers),
                        request_headers=_optional_dict(request_headers),
                        ttl=None if ttl is None else format_duration(ttl),
                        expires_at=expires_at,
                    )
                ),
            )
        return _preview_info_from_api(preview)

    async def delete(self, *, name: str) -> SandboxPreviewInfo:
        """Delete a preview and its tokens, returning its record.

        The URL stops serving at once, but the record may show the preview as
        deleting while the server finishes; this does not wait for that.

        Args:
            name: Name of the preview.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            preview = await api.delete_sandbox_preview(
                sandbox_name=sandbox_name,
                preview_name=name,
                params=baseten.client.managementapi.DeleteSandboxPreviewParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )
        return _preview_info_from_api(preview)

    async def create_token(
        self,
        *,
        preview_name: str,
        expires_at: datetime,
        create_if_not_exists: bool | None = None,
    ) -> SandboxPreviewTokenInfo:
        """Create a token granting access to a private preview.

        Args:
            preview_name: Name of the private preview.
            expires_at: When the token stops working.
            create_if_not_exists: When true, return an existing unexpired token
                of the preview that stays valid at least until *expires_at*,
                instead of creating one. A token created moments before may not
                be found yet, so a new one may be created anyway.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            token = await api.create_sandbox_preview_token(
                sandbox_name=sandbox_name,
                preview_name=preview_name,
                params=baseten.client.managementapi.CreateSandboxPreviewTokenParams(
                    **set_fields(team_id=self._context.team_id)
                ),
                request=baseten.client.managementapi.CreateSandboxPreviewTokenRequest(
                    expires_at=expires_at,
                    **set_fields(create_if_not_exists=create_if_not_exists),
                ),
            )
        return _preview_token_info_from_api(token)

    async def list_tokens(
        self, *, preview_name: str, page_size: int | None = None
    ) -> AsyncIterator[SandboxPreviewTokenInfo]:
        """List a private preview's tokens, fetching further pages as iteration reaches them.

        Expired tokens are included until the server removes them. A public
        preview has no tokens, and the server rejects the call.

        Args:
            preview_name: Name of the private preview.
            page_size: How many tokens to fetch per underlying request.
        """
        sandbox_name = _sandbox_name(self._context.name)

        async def fetch_page(
            cursor: str | None,
        ) -> baseten.client.managementapi.ListSandboxPreviewTokensResponse:
            with self._context.control_api() as api:
                return await api.list_sandbox_preview_tokens(
                    sandbox_name=sandbox_name,
                    preview_name=preview_name,
                    params=baseten.client.managementapi.ListSandboxPreviewTokensParams(
                        **set_fields(
                            team_id=self._context.team_id,
                            cursor=cursor,
                            limit=page_size,
                        )
                    ),
                )

        async for token in paginate_async("preview token list", fetch_page):
            yield _preview_token_info_from_api(token)

    async def delete_token(self, *, preview_name: str, token_name: str) -> None:
        """Revoke a preview token. Requests using it are rejected at once.

        Args:
            preview_name: Name of the private preview.
            token_name: Name of the token.
        """
        sandbox_name = _sandbox_name(self._context.name)
        with self._context.control_api() as api:
            await api.delete_sandbox_preview_token(
                sandbox_name=sandbox_name,
                preview_name=preview_name,
                token_name=token_name,
                params=baseten.client.managementapi.DeleteSandboxPreviewTokenParams(
                    **set_fields(team_id=self._context.team_id)
                ),
            )


def _sandbox_name(name: str) -> str:
    """Return the sandbox's name, failing for a sandbox from a URL alone."""
    if name == "":
        raise ValueError(
            "previews need the sandbox's name, which a sandbox from "
            "sandbox_from_url does not have; use SandboxClient.get"
        )
    return name


def _optional_dict(values: Mapping[str, str] | None) -> dict[str, str] | None:
    """Copy a mapping to a dict, keeping ``None``."""
    return None if values is None else dict(values)


def _preview_info_from_api(
    preview: baseten.client.managementapi.SandboxPreview,
) -> SandboxPreviewInfo:
    """Convert the control plane's preview record."""
    return SandboxPreviewInfo(
        name=preview.name,
        port=preview.port,
        url=str(preview.url),
        status=preview.status,
        public=preview.public,
        response_headers=dict(preview.response_headers or {}),
        request_headers=dict(preview.request_headers or {}),
        ttl=None if not preview.ttl else parse_duration(preview.ttl, "preview TTL"),
        expires_at=preview.expires_at,
        region=preview.region,
        created_at=preview.created_at,
        updated_at=preview.updated_at,
        created_by=preview.created_by,
        updated_by=preview.updated_by,
    )


def _preview_token_info_from_api(
    token: baseten.client.managementapi.SandboxPreviewToken,
) -> SandboxPreviewTokenInfo:
    """Convert the control plane's preview token record."""
    return SandboxPreviewTokenInfo(
        name=token.name,
        token=token.token,
        expires_at=token.expires_at,
        expired=token.expired,
    )

"""Construct Lattice SDK clients from :class:`LatticeSettings`.

Environment token is the default auth (passed as a callable, per the SDK). OAuth client credentials
are used only when no environment token is set. The sandbox header is set on the client constructor so
it also covers the OAuth token fetch.
"""

from __future__ import annotations

from anduril import AsyncLattice, Lattice

from .config import ConfigError, LatticeSettings

SANDBOX_HEADER = "anduril-sandbox-authorization"


def _headers(settings: LatticeSettings) -> dict[str, str] | None:
    if settings.sandboxes_token:
        return {SANDBOX_HEADER: f"Bearer {settings.sandboxes_token}"}
    return None


def _auth_kwargs(settings: LatticeSettings) -> dict:
    if settings.environment_token:
        token = settings.environment_token
        return {"token": lambda: token}
    if settings.client_id and settings.client_secret:
        return {"client_id": settings.client_id, "client_secret": settings.client_secret}
    raise ConfigError("missing ENVIRONMENT_TOKEN (or LATTICE_CLIENT_ID + LATTICE_CLIENT_SECRET)")


def make_client(settings: LatticeSettings | None = None, *, timeout: float | None = None) -> Lattice:
    """Synchronous ``anduril.Lattice`` client. ``timeout`` (seconds) defaults to the SDK's 60."""
    settings = settings or LatticeSettings.from_env()
    return Lattice(
        base_url=settings.base_url,
        headers=_headers(settings),
        timeout=timeout,
        **_auth_kwargs(settings),
    )


def make_async_client(
    settings: LatticeSettings | None = None, *, timeout: float | None = None
) -> AsyncLattice:
    """Asynchronous ``anduril.AsyncLattice`` client with the same auth rules as :func:`make_client`."""
    settings = settings or LatticeSettings.from_env()
    return AsyncLattice(
        base_url=settings.base_url,
        headers=_headers(settings),
        timeout=timeout,
        **_auth_kwargs(settings),
    )

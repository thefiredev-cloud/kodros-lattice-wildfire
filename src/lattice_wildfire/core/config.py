"""Settings loaded from the environment (optionally a ``.env`` file). Values are never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

# LA County bounding box: min_lon,min_lat,max_lon,max_lat (the ArcGIS envelope order).
# South edge 33.28 includes Santa Catalina Island (LACoFD jurisdiction); San Clemente Island is excluded.
DEFAULT_AOI_BBOX = "-118.95,33.28,-117.65,34.82"


class ConfigError(ValueError):
    """Missing or malformed configuration. Messages name variables, never their values."""


def _get(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True, repr=False)
class LatticeSettings:
    base_url: str
    environment_token: str | None
    sandboxes_token: str | None
    client_id: str | None
    client_secret: str | None

    @classmethod
    def from_env(cls, *, dotenv: bool = True) -> LatticeSettings:
        """Read Lattice settings from the environment.

        A ``.env`` file found from the current directory upward is loaded first without overriding
        variables that are already set. ``LATTICE_BASE_URL`` (a full URL, used for the local harness)
        wins over ``LATTICE_ENDPOINT`` (a bare hostname, turned into ``https://<host>``).
        Credentials: ``ENVIRONMENT_TOKEN``, or ``LATTICE_CLIENT_ID`` + ``LATTICE_CLIENT_SECRET``.
        ``SANDBOXES_TOKEN`` is optional and only needed for Lattice Sandboxes.
        """
        if dotenv:
            path = find_dotenv(usecwd=True)
            if path:
                load_dotenv(path, override=False)

        problems: list[str] = []
        base_url = _get("LATTICE_BASE_URL")
        endpoint = _get("LATTICE_ENDPOINT")
        if base_url is not None:
            if not base_url.startswith(("http://", "https://")):
                problems.append("LATTICE_BASE_URL must start with http:// or https://")
            base_url = base_url.rstrip("/")
        elif endpoint is not None:
            if "://" in endpoint:
                problems.append(
                    "LATTICE_ENDPOINT must be a bare hostname without a scheme "
                    "(remove the http(s):// prefix, or set LATTICE_BASE_URL instead)"
                )
            elif "/" in endpoint.rstrip("/"):
                problems.append("LATTICE_ENDPOINT must be a bare hostname without a path")
            base_url = f"https://{endpoint.rstrip('/')}"
        else:
            problems.append("missing LATTICE_ENDPOINT (or LATTICE_BASE_URL)")

        environment_token = _get("ENVIRONMENT_TOKEN")
        client_id = _get("LATTICE_CLIENT_ID")
        client_secret = _get("LATTICE_CLIENT_SECRET")
        if environment_token is None:
            if client_id is None and client_secret is None:
                problems.append("missing ENVIRONMENT_TOKEN (or LATTICE_CLIENT_ID + LATTICE_CLIENT_SECRET)")
            elif client_id is None:
                problems.append("missing LATTICE_CLIENT_ID (LATTICE_CLIENT_SECRET is set)")
            elif client_secret is None:
                problems.append("missing LATTICE_CLIENT_SECRET (LATTICE_CLIENT_ID is set)")

        if problems:
            raise ConfigError("Lattice configuration invalid: " + "; ".join(problems))
        assert base_url is not None
        return cls(
            base_url=base_url,
            environment_token=environment_token,
            sandboxes_token=_get("SANDBOXES_TOKEN"),
            client_id=client_id,
            client_secret=client_secret,
        )

    @property
    def auth_mode(self) -> str:
        """``"environment-token"`` or ``"client-credentials"``."""
        return "environment-token" if self.environment_token else "client-credentials"

    def __repr__(self) -> str:
        def flag(value: str | None) -> str:
            return "<set>" if value else "None"

        return (
            f"LatticeSettings(base_url={self.base_url!r}, environment_token={flag(self.environment_token)}, "
            f"sandboxes_token={flag(self.sandboxes_token)}, client_id={flag(self.client_id)}, "
            f"client_secret={flag(self.client_secret)})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class AOI:
    """Area of interest as a lat/lon bounding box (degrees, WGS84)."""

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def __post_init__(self) -> None:
        if not (-90.0 <= self.min_lat < self.max_lat <= 90.0):
            raise ConfigError("AOI latitudes must satisfy -90 <= min_lat < max_lat <= 90")
        if not (-180.0 <= self.min_lon < self.max_lon <= 180.0):
            raise ConfigError("AOI longitudes must satisfy -180 <= min_lon < max_lon <= 180")

    def envelope(self) -> tuple[float, float, float, float]:
        """ArcGIS envelope order: (xmin=min_lon, ymin=min_lat, xmax=max_lon, ymax=max_lat)."""
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)


def aoi_from_env() -> AOI:
    """``LW_AOI_BBOX="min_lon,min_lat,max_lon,max_lat"``; defaults to LA County."""
    raw = _get("LW_AOI_BBOX") or DEFAULT_AOI_BBOX
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ConfigError("LW_AOI_BBOX must be four comma-separated numbers: min_lon,min_lat,max_lon,max_lat")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise ConfigError("LW_AOI_BBOX must contain numbers only: min_lon,min_lat,max_lon,max_lat") from exc
    return AOI(min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon)

"""Read-only local operating picture for the Lattice wildfire picture (``lw-map``).

The HTTP server is stdlib only and binds loopback by default; the front end is a vendored Leaflet
1.9.4 map with no runtime CDN dependency. See ``docs/apps/map.md``.
"""

from __future__ import annotations

from .server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    INTEGRATION,
    LAYERS,
    BindRefused,
    PictureService,
    brief_path,
    create_server,
    feature_for,
    is_loopback,
    picture_collection,
    read_brief,
    resolve_host,
    serve,
    tasks_payload,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "INTEGRATION",
    "LAYERS",
    "BindRefused",
    "PictureService",
    "brief_path",
    "create_server",
    "feature_for",
    "is_loopback",
    "picture_collection",
    "read_brief",
    "resolve_host",
    "serve",
    "tasks_payload",
]

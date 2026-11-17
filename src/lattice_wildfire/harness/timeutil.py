"""RFC 3339 helpers for the harness (nanosecond fractions are truncated to microseconds)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(value: str) -> datetime:
    """Parse an RFC 3339 timestamp; naive values are rejected because Lattice requires an offset."""
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset")
    return parsed.astimezone(UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now() -> datetime:
    return datetime.now(UTC)

"""Shared fixtures: a session-wide Lattice contract harness and an SDK client pointed at it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from anduril import Lattice

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.harness import Harness, start_harness

FIXTURES = Path(__file__).parent / "fixtures"

_LATTICE_VARS = (
    "LATTICE_BASE_URL",
    "LATTICE_ENDPOINT",
    "ENVIRONMENT_TOKEN",
    "SANDBOXES_TOKEN",
    "LATTICE_CLIENT_ID",
    "LATTICE_CLIENT_SECRET",
)


@pytest.fixture(scope="session")
def harness() -> Iterator[Harness]:
    """In-memory Lattice REST harness shared by the whole test session (long polls held 2 s)."""
    h = start_harness(0, long_poll_timeout_s=2.0)
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture
def lattice_env(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Environment pointing ``LatticeSettings.from_env()`` at the harness with a dummy token."""
    for name in _LATTICE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATTICE_BASE_URL", harness.base_url)
    monkeypatch.setenv("ENVIRONMENT_TOKEN", "test-token")
    return harness


@pytest.fixture
def lattice(lattice_env: Harness) -> Lattice:
    """Real ``anduril.Lattice`` client built by ``make_client`` from the monkeypatched environment."""
    return make_client(LatticeSettings.from_env(dotenv=False), timeout=30)

"""Local, in-memory Lattice REST contract server for offline tests (``python -m lattice_wildfire.harness``)."""

from .server import Harness, start_harness

__all__ = ["Harness", "start_harness"]

from __future__ import annotations

import pytest

from lattice_wildfire.core import client
from lattice_wildfire.core.config import ConfigError, LatticeSettings


def _settings(**overrides) -> LatticeSettings:
    values = {
        "base_url": "http://127.0.0.1:8765",
        "environment_token": "environment-token",
        "sandboxes_token": "sandbox-token",
        "client_id": None,
        "client_secret": None,
    }
    values.update(overrides)
    return LatticeSettings(**values)


def test_sync_client_uses_environment_token_and_sandbox_header(monkeypatch):
    captured = {}
    monkeypatch.setattr(client, "Lattice", lambda **kwargs: captured.update(kwargs) or "sync")

    result = client.make_client(_settings(), timeout=12.5)

    assert result == "sync"
    assert captured["base_url"] == "http://127.0.0.1:8765"
    assert captured["headers"] == {client.SANDBOX_HEADER: "Bearer sandbox-token"}
    assert captured["timeout"] == 12.5
    assert captured["token"]() == "environment-token"
    assert "client_id" not in captured


def test_async_client_uses_client_credentials_without_sandbox_header(monkeypatch):
    captured = {}
    monkeypatch.setattr(client, "AsyncLattice", lambda **kwargs: captured.update(kwargs) or "async")
    settings = _settings(
        environment_token=None,
        sandboxes_token=None,
        client_id="client-id",
        client_secret="client-secret",
    )

    result = client.make_async_client(settings)

    assert result == "async"
    assert captured["headers"] is None
    assert captured["timeout"] is None
    assert captured["client_id"] == "client-id"
    assert captured["client_secret"] == "client-secret"
    assert "token" not in captured


def test_client_rejects_missing_authentication_credentials():
    settings = _settings(environment_token=None)

    with pytest.raises(ConfigError, match="missing ENVIRONMENT_TOKEN"):
        client.make_client(settings)

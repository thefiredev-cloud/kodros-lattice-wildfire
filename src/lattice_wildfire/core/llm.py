"""Client for the local Kodros cluster LLM (OpenAI-compatible). No hosted fallback exists by design."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "cluster-current"
DEFAULT_TIMEOUT_S = 180.0


class LLMUnavailable(RuntimeError):
    """The cluster LLM could not be reached or returned an unusable response."""


@dataclass(frozen=True)
class ChatResult:
    """One completion plus the provenance a caller should record alongside it.

    ``model`` is the id the server reported in the response body (for the Kodros cluster that is the
    loaded weights, e.g. ``DeepSeek-V4.1-Flash-1017-EXL3``), not the alias that was requested.
    """

    text: str
    model: str
    latency_s: float
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    """One entry of ``/v1/models``. ``loaded_model`` is set by engines that serve an alias."""

    id: str
    owned_by: str | None = None
    loaded_model: str | None = None


def base_url() -> str:
    """``${KODROS_LLM_URL:-http://127.0.0.1:8080/v1}`` without a trailing slash."""
    return (os.environ.get("KODROS_LLM_URL") or DEFAULT_URL).strip().rstrip("/")


def default_model() -> str:
    """``${KODROS_LLM_MODEL:-cluster-current}``."""
    return (os.environ.get("KODROS_LLM_MODEL") or DEFAULT_MODEL).strip()


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def models(*, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[ModelInfo]:
    """GET ``/v1/models``. Raises :class:`LLMUnavailable` on failure or an empty listing."""
    try:
        resp = httpx.get(f"{base_url()}/models", timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMUnavailable(f"cluster LLM at {base_url()} failed: {type(exc).__name__}: {exc}") from exc
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise LLMUnavailable("cluster LLM /models returned no data list")
    out = [
        ModelInfo(
            id=row["id"].strip(),
            owned_by=_optional_str(row.get("owned_by")),
            loaded_model=_optional_str(row.get("loaded_model")),
        )
        for row in rows
        if isinstance(row, dict) and _optional_str(row.get("id"))
    ]
    if not out:
        raise LLMUnavailable("cluster LLM /models listed no usable model id")
    return out


def complete(payload: dict[str, Any], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """POST a raw ``/chat/completions`` payload and return the response JSON.

    Use this for features :func:`chat_result` does not cover (tool calling, JSON mode). ``model``
    defaults to :func:`default_model` when absent from ``payload``.
    """
    body = {"model": default_model(), **payload}
    try:
        resp = httpx.post(f"{base_url()}/chat/completions", json=body, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMUnavailable(f"cluster LLM at {base_url()} failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("choices"):
        raise LLMUnavailable("cluster LLM returned no choices")
    return data


def chat_result(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.2,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ChatResult:
    """Single chat completion with latency and provenance.

    Raises :class:`LLMUnavailable` on failure, on an empty message, or when the response carries no
    server-reported model id (provenance would then be unknown).
    """
    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if model:
        payload["model"] = model
    started = time.perf_counter()
    data = complete(payload, timeout_s=timeout_s)
    latency_s = time.perf_counter() - started
    message = (data["choices"][0] or {}).get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMUnavailable("cluster LLM returned an empty message")
    served = _optional_str(data.get("model"))
    if served is None:
        raise LLMUnavailable("cluster LLM response has no model id; provenance would be unknown")
    usage = data.get("usage")
    return ChatResult(
        text=content.strip(),
        model=served,
        latency_s=latency_s,
        usage=usage if isinstance(usage, dict) else {},
    )


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.2,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Single chat completion; returns the assistant text. See :func:`chat_result` for provenance."""
    return chat_result(
        messages, model=model, max_tokens=max_tokens, temperature=temperature, timeout_s=timeout_s
    ).text

"""`ask`: answer one question with the local cluster model calling the Lattice Talk tools.

The cluster (TensorFold, OpenAI-compatible) supports native `tools` / `tool_calls`; probed
2026-11-09 against `cluster-current` (DeepSeek-V4.1-Flash-1017-EXL3). So this loop uses native
OpenAI tool calling, not a JSON-action fallback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lattice_wildfire.core import llm
from lattice_wildfire.talk.tools import TOOL_SPECS, TalkTools

SYSTEM_PROMPT = """You answer questions about the live Lattice wildfire picture for Los Angeles County.
Rules:
- Call tools to get facts. Use only facts that appear in tool results. Never invent entities, numbers or times.
- Cite every entity you mention by its entity_id in square brackets, for example [lw-station-11].
- Give distances in km with the bearing when the tool returns one.
- If the tools return nothing relevant, say that plainly.
- request_investigation is a dry run unless confirm is true. Only request one when the user asks for it.
- Keep the answer short: a direct answer first, then at most five bullet points."""

ENTITY_ID_RE = re.compile(r"\[(lw-[A-Za-z0-9._:-]+)\]")

# messages, tool specs (or None for a final no-tools turn) -> assistant message dict
Completion = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]]


def cluster_completion(
    *, model: str | None = None, max_tokens: int = 900, temperature: float = 0.1
) -> Completion:
    """Completion backed by the local Kodros cluster (`core.llm.complete`). Raises LLMUnavailable."""

    def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if model:
            payload["model"] = model
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        message = (llm.complete(payload)["choices"][0] or {}).get("message")
        if not isinstance(message, dict):
            raise llm.LLMUnavailable("cluster LLM returned a choice without a message")
        return message

    return complete


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class AskResult:
    answer: str
    rounds: int
    calls: list[ToolCallRecord] = field(default_factory=list)
    cited_ids: list[str] = field(default_factory=list)
    unsupported_ids: list[str] = field(default_factory=list)
    """Ids cited in the answer that never appeared in any tool result."""


def cited_entity_ids(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in ENTITY_ID_RE.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def ids_in(value: Any) -> set[str]:
    """Every entity/task id string found anywhere in a tool result."""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= ids_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= ids_in(v)
    elif isinstance(value, str) and value.startswith("lw-"):
        found.add(value)
    return found


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" ...[truncated {len(text) - limit} chars]"


def run_ask(
    question: str,
    tools: TalkTools,
    complete: Completion,
    *,
    allow_tasking: bool = False,
    max_rounds: int = 6,
    max_tool_chars: int = 12_000,
) -> AskResult:
    """Tool-calling loop. `request_investigation` is forced to a dry run unless `allow_tasking`."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    calls: list[ToolCallRecord] = []
    for round_no in range(1, max_rounds + 1):
        reply = complete(messages, TOOL_SPECS)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            return _finish(reply.get("content") or "", round_no, calls)
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                arguments = _parse_arguments(fn.get("arguments"))
            except ValueError as exc:
                arguments, result = {}, {"error": f"arguments are not a JSON object: {exc}"}
            else:
                if name == "request_investigation" and not allow_tasking and arguments.get("confirm"):
                    arguments = {**arguments, "confirm": False}
                    result = tools.call(name, arguments)
                    result["note"] = (
                        "Tasking is disabled in this session (no --confirm-tasking). This was a dry run."
                    )
                else:
                    result = tools.call(name, arguments)
            calls.append(ToolCallRecord(name=name, arguments=arguments, result=result))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": _clip(json.dumps(result, default=str), max_tool_chars),
                }
            )
    final = complete(
        [*messages, {"role": "user", "content": "Answer now from the tool results above."}], None
    )
    return _finish(final.get("content") or "", max_rounds + 1, calls)


def _finish(answer: str, rounds: int, calls: list[ToolCallRecord]) -> AskResult:
    known: set[str] = set()
    for record in calls:
        known |= ids_in(record.result)
    cited = cited_entity_ids(answer)
    return AskResult(
        answer=answer.strip(),
        rounds=rounds,
        calls=calls,
        cited_ids=cited,
        unsupported_ids=[i for i in cited if i not in known],
    )

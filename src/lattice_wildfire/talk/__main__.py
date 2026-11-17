"""Lattice Talk CLI.

    python -m lattice_wildfire.talk ask "What is burning within 10 km of station 11?"
    python -m lattice_wildfire.talk call near '{"lat": 34.15, "lon": -118.14, "radius_km": 5}'
    python -m lattice_wildfire.talk tools

`ask` runs the local Kodros cluster model with OpenAI tool calling over the shared tools.
`request_investigation` stays a dry run in `ask` unless `--confirm-tasking` is given, and in
`call` unless the JSON arguments set `"confirm": true`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from lattice_wildfire.core.config import ConfigError

EXIT_CONFIG = 2
EXIT_LLM = 3
EXIT_TOOL_ERROR = 4


def _tools() -> Any:
    from lattice_wildfire.core.client import make_client
    from lattice_wildfire.talk.tools import TalkTools

    return TalkTools(make_client())


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def cmd_tools(_args: argparse.Namespace) -> int:
    from lattice_wildfire.talk.tools import TOOL_SPECS

    for spec in TOOL_SPECS:
        fn = spec["function"]
        params = fn["parameters"]
        args = ", ".join(
            f"{name}{'' if name in params['required'] else '?'}" for name in params["properties"]
        )
        print(f"{fn['name']}({args})\n    {fn['description']}")
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    try:
        arguments = json.loads(args.json_args) if args.json_args else {}
    except ValueError as exc:
        print(f"lw-talk: JSON_ARGS is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not isinstance(arguments, dict):
        print("lw-talk: JSON_ARGS must be a JSON object", file=sys.stderr)
        return EXIT_CONFIG
    result = _tools().call(args.tool, arguments)
    print(_dump(result))
    return EXIT_TOOL_ERROR if "error" in result else 0


def cmd_ask(args: argparse.Namespace) -> int:
    from lattice_wildfire.core.llm import LLMUnavailable
    from lattice_wildfire.talk.ask import cluster_completion, run_ask

    tools = _tools()
    try:
        result = run_ask(
            args.question,
            tools,
            cluster_completion(model=args.model),
            allow_tasking=args.confirm_tasking,
            max_rounds=args.max_rounds,
        )
    except LLMUnavailable as exc:
        print(f"lw-talk: local model unavailable, no answer given: {exc}", file=sys.stderr)
        return EXIT_LLM
    if args.json:
        print(
            _dump(
                {
                    "answer": result.answer,
                    "rounds": result.rounds,
                    "cited_ids": result.cited_ids,
                    "unsupported_ids": result.unsupported_ids,
                    "calls": [
                        {"name": c.name, "arguments": c.arguments, "result": c.result} for c in result.calls
                    ],
                }
            )
        )
        return 0
    if args.show_tools:
        for c in result.calls:
            print(f"> {c.name}({json.dumps(c.arguments)})", file=sys.stderr)
    print(result.answer)
    if result.unsupported_ids:
        print(
            "warning: answer cites ids no tool returned: " + ", ".join(result.unsupported_ids),
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lw-talk", description="Talk to the Lattice wildfire picture.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ask", help="answer a question with the local cluster model and the tools")
    p.add_argument("question")
    p.add_argument("--confirm-tasking", action="store_true", help="let the model create Investigate tasks")
    p.add_argument("--max-rounds", type=int, default=6)
    p.add_argument("--model", default=None, help="cluster model id (default KODROS_LLM_MODEL)")
    p.add_argument("--show-tools", action="store_true", help="print tool calls to stderr")
    p.add_argument("--json", action="store_true", help="print answer, citations and tool calls as JSON")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("call", help="run one tool directly and print its JSON result")
    p.add_argument("tool")
    p.add_argument("json_args", nargs="?", default="{}")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("tools", help="list the tools")
    p.set_defaults(fn=cmd_tools)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"lw-talk: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())

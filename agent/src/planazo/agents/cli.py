"""The `planazo-agent` console entrypoint.

Runs the event-discovery agent's observe -> reason -> act -> verify loop
against the real LLM for any prompt — one-shot (`planazo-agent "<prompt>"`)
or an interactive REPL when no prompt is given. The model decides whether
to call a tool via native tool-calling; each tool invocation is printed as
it fires, then the final answer, step count, and stop reason follow. Model
role is selectable through `agentlib` (default the cheap role;
`--strong`/`--model` override) with no raw model id in this module.

Every invocation gates the irreversible tool
(`confirm_and_create_calendar_event`) behind a one-line terminal `y/N`
prompt; the reversible tool (`save_event_candidate`) runs without a prompt.
Declining a gated call skips the tool and tells the model the request was
declined so it can adjust its answer.

`import openai` here names `openai.OpenAIError` for the narrow `except`
around the LLM call — it makes no provider call itself (those go through
`agentlib`).
"""

import argparse
import os
import sys
from typing import Any

import openai

from agentlib.core import MODELS
from planazo.agents.event_agent import run_once
from planazo.agents.loop import ApprovalGate, LoopResult, StepRecord
from tools.tools import IRREVERSIBLE_TOOLS

_MISSING_KEY_MESSAGE = (
    "OPENCODE_API_KEY is not set. Copy ../.env.example to a .env file at the "
    "repo root and set OPENCODE_API_KEY."
)


def _positive_int(value: str) -> int:
    """Argparse type for `--max-steps`: reject values below 1 cleanly."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planazo-agent",
        description="Run Planazo's event-discovery agent loop against the LLM.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="the prompt to run one-shot; omit to open an interactive REPL",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--strong",
        action="store_true",
        help="use the strong model role",
    )
    group.add_argument(
        "--model",
        choices=["cheap", "strong"],
        help="select the model role by name (default: cheap)",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=None,
        help="cap the number of loop steps (default: run_once's own default)",
    )
    return parser


def _format_step(record: StepRecord) -> str:
    """One trace line for a dispatched tool call."""
    return f"  step {record.step}: {record.tool}({record.arguments}) -> {record.result}"


def _render_result(result: LoopResult) -> str:
    """The final block: the answer (or a truncation/max-steps notice) and the tally.

    A truncated stop carries a non-None partial `answer`, so it is labelled
    before the plain-answer branch — otherwise a cut-off answer would render as
    a trustworthy complete one.
    """
    if result.stopped == "truncated":
        body = f"partial answer (truncated by output cap): {result.answer}"
    elif result.answer is not None:
        body = f"answer: {result.answer}"
    else:
        body = "(no final answer — hit max steps)"
    return f"\n{body}\nsteps: {result.steps} | stop reason: {result.stopped}"


def _print_step(record: StepRecord) -> None:
    print(_format_step(record))


def _terminal_approve(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Prompt the operator at the terminal for approval to run a gated tool.

    Reads one line from stdin. Any `y`/`yes` (case-insensitive, stripped)
    approves; anything else declines. A broken stdin (`EOFError` /
    `KeyboardInterrupt`) declines — never approves — because a missing human
    is not a yes.
    """
    prompt = f"approval required: {tool_name}({arguments}) — approve? [y/N]: "
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def _run(prompt: str, *, model: str, max_steps: int | None) -> int:
    """Run one prompt, printing the live trace then the result block.

    Returns 0 on success, 1 if the provider raised. Only `openai.OpenAIError`
    is caught here — an unexpected error propagates as a real traceback.
    """
    gate = ApprovalGate(tool_names=frozenset(IRREVERSIBLE_TOOLS), approve=_terminal_approve)
    try:
        if max_steps is None:
            result = run_once(prompt, model=model, on_step=_print_step, gate=gate)
        else:
            result = run_once(
                prompt, model=model, on_step=_print_step, max_steps=max_steps, gate=gate
            )
    except openai.OpenAIError as exc:
        print(str(exc))
        return 1
    print(_render_result(result))
    return 0


def _repl(*, model: str, max_steps: int | None) -> int:
    """Read prompts until exit/quit, EOF, or Ctrl-C; one run_once per line."""
    while True:
        try:
            line = input("agent> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in ("exit", "quit"):
            print("bye")
            return 0
        _run(stripped, model=model, max_steps=max_steps)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    role = "strong" if args.strong else (args.model or "cheap")
    model = MODELS[role]

    if not os.environ.get("OPENCODE_API_KEY"):
        print(_MISSING_KEY_MESSAGE)
        return 1

    if args.prompt is None:
        return _repl(model=model, max_steps=args.max_steps)
    return _run(args.prompt, model=model, max_steps=args.max_steps)


if __name__ == "__main__":
    sys.exit(main())

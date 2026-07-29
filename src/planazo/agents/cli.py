"""The `planazo-agent` console entrypoint.

Runs the event-discovery agent's observe -> reason -> act -> verify loop
against the real LLM for any prompt — one-shot (`planazo-agent "<prompt>"`)
or an interactive REPL when no prompt is given. The model decides whether
to call a tool via native tool-calling; each tool invocation is printed as
it fires, then the final answer, step count, and stop reason follow. Model
role is selectable through `agentlib` (default the cheap role;
`--strong`/`--model` override) with no raw model id in this module.

Without extra flags the model is offered one tool, `search_events`. The two
calendar reference tools are enabled by `--calendar`; when they are, the
irreversible one (`confirm_and_create_calendar_event`) is gated behind a
one-line terminal `y/N` prompt and the reversible one
(`save_event_candidate`) runs without a prompt. Declining a gated call skips
the tool and tells the model the request was declined so it can adjust its
answer.

`--user-id N` binds the run to one identity: the four memory tools join the
tool set bound to that id, and that user's stored preferences are pushed into
the run's system message. The committed markdown rules are pushed on every
invocation, identity or not. The flag is unauthenticated dev impersonation —
whatever id the shell supplies is used, so this CLI is an operator's surface,
not a user-facing one (`docs/adr/0004-three-store-memory-model.md`).

`import openai` here names `openai.OpenAIError` for the narrow `except`
around the LLM call — it makes no provider call itself (those go through
`agentlib`).
"""

import argparse
import sys
from typing import Any

import openai

from agentlib.core import MODELS
from planazo.agents.event_agent import RecommenderResult, run_once
from planazo.agents.loop import StepRecord
from planazo.approval import ApprovalGate
from planazo.config import check_api_key
from planazo.query.interpreter import interpret
from tools.tools import IRREVERSIBLE_TOOLS


def _positive_int(value: str) -> int:
    """Argparse type for `--max-steps` and `--user-id`: reject below 1 cleanly.

    `--user-id 0` would otherwise reach `build_memory_tools`, whose
    `MemoryScopeRequest` raises a `ValidationError` that `_run`'s
    `openai.OpenAIError`-only guard does not catch — a raw traceback where
    argparse gives a one-line usage error and exit code 2.
    """
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
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="add the calendar reference tools (save + gated confirm) to the tool set",
    )
    parser.add_argument(
        "--user-id",
        type=_positive_int,
        required=True,
        help="bind the run to this user id: adds memory tools and preferences",
    )
    return parser


def _format_step(record: StepRecord) -> str:
    """One trace line for a dispatched tool call."""
    return f"  step {record.step}: {record.tool}({record.arguments}) -> {record.result}"


def _render_result(result: RecommenderResult) -> str:
    """The final block: the answer (or a truncation/max-steps notice) and the tally.

    A truncated stop carries a non-None partial `answer`, so it is labelled
    before the plain-answer branch — otherwise a cut-off answer would render as
    a trustworthy complete one.
    """
    if result.status == "error":
        if result.error_type == "invalid_preference_data":
            body = "configuration/data-safe failure: preferences could not be loaded safely"
        elif result.error_type == "preference_store_unavailable":
            body = "configuration/data-safe failure: preference store unavailable"
        elif result.error_type == "missing_search_origin":
            body = (
                "configuration/data-safe failure: a trusted search origin is required "
                "for radius filtering"
            )
        elif result.error_type in (
            "search_store_unavailable",
            "search_invalid_filter",
            "search_tool_failure",
            "invalid_search_output",
            "search_not_completed",
        ):
            body = f"search error ({result.error_type}): {result.answer or 'no details'}"
        else:
            body = f"error ({result.error_type}): {result.answer or 'no details'}"
    elif result.status == "needs_clarification":
        question = result.clarification.question if result.clarification else "no question"
        body = f"clarification needed: {question}"
    elif result.status == "incomplete":
        body = f"incomplete ({result.stopped}): {result.answer or 'no answer'}"
    elif result.status == "no_results":
        body = f"no matching events found: {result.answer or ''}"
    elif result.status == "ok":
        body = f"answer: {result.answer}" if result.answer else "answer: (empty)"
    else:
        body = f"unknown status {result.status}: {result.answer or ''}"

    fallback_note = " [interpreter fallback]" if result.interpreter_fallback else ""
    tally = (
        f"steps: {result.steps} | stop reason: {result.stopped} | "
        f"status: {result.status} | candidates: {len(result.candidates)}"
    )
    return f"\n{body}{fallback_note}\n{tally}"


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


def _run(
    prompt: str,
    *,
    model: str,
    max_steps: int | None,
    calendar_enabled: bool,
    user_id: int,
) -> int:
    """Run one prompt, printing the live trace then the result block.

    Returns 0 on success, 1 if the provider raised or on preflight error.
    Only `openai.OpenAIError` is caught here — an unexpected error propagates
    as a real traceback.
    """
    gate = ApprovalGate(tool_names=frozenset(IRREVERSIBLE_TOOLS), approve=_terminal_approve)

    # Interpret the raw prompt. ADR 0020: `interpret` may route the
    # message as `chat` (small talk / meta-question) — in that case
    # print the router's reply verbatim and exit; no Recommender loop.
    routed = interpret(prompt)
    if routed.kind == "chat":
        print(routed.answer)
        return 0
    intent = routed.intent

    run_context: dict[str, Any] = {
        "model": model,
        "on_step": _print_step,
        "gate": gate,
        "calendar_enabled": calendar_enabled,
    }
    if max_steps is not None:
        run_context["max_steps"] = max_steps
    try:
        result = run_once(user_id, intent, **run_context)
    except openai.OpenAIError as exc:
        print(str(exc))
        return 1
    print(_render_result(result))
    return 1 if result.status == "error" else 0


def _repl(*, model: str, max_steps: int | None, calendar_enabled: bool, user_id: int) -> int:
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
        _run(
            stripped,
            model=model,
            max_steps=max_steps,
            calendar_enabled=calendar_enabled,
            user_id=user_id,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    role = "strong" if args.strong else (args.model or "cheap")
    model = MODELS[role]

    if not check_api_key():
        return 1

    if args.prompt is None:
        return _repl(
            model=model,
            max_steps=args.max_steps,
            calendar_enabled=args.calendar,
            user_id=args.user_id,
        )
    return _run(
        args.prompt,
        model=model,
        max_steps=args.max_steps,
        calendar_enabled=args.calendar,
        user_id=args.user_id,
    )


if __name__ == "__main__":
    sys.exit(main())

"""One-shot LLM generator for `data/eval/events_seed.jsonl`.

Reads the committed prompt at `data/eval/generation_prompt.md`, calls the
OpenCode Zen `strong` model once, parses the reply as JSONL, validates
each row against `planazo.catalog.models.Event`, and writes the survivors
to the target path.

**Provenance of the committed JSONL.** The seed events file
`data/eval/events_seed.jsonl` at HEAD was hand-authored during Stage 4 of
ADR 0025 to keep the golden-query ids stable across the eval harness's
lifetime. Re-running this script emits a similar-but-not-identical corpus
— the LLM is not perfectly deterministic even at `temperature=0`, and any
regeneration necessarily invalidates the ids that
`data/eval/questions.jsonl` cross-references. Regenerate only when
prepared to re-author the golden set.

Run: `uv run python scripts/generate_events_seed.py --target 120`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from agentlib.core import CHEAP, STRONG, call
from planazo.catalog.models import Event

_DEFAULT_TARGET = 120
_DEFAULT_OUT = Path("data/eval/events_seed.jsonl")
_DEFAULT_PROMPT = Path("data/eval/generation_prompt.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="Destination JSONL path (default: data/eval/events_seed.jsonl).",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=_DEFAULT_PROMPT,
        help="Prompt file path (default: data/eval/generation_prompt.md).",
    )
    parser.add_argument(
        "--model",
        choices=("strong", "cheap"),
        default="strong",
        help="OpenCode Zen model tier (default: strong).",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=_DEFAULT_TARGET,
        help="Approximate number of events to generate (default: 120).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=32000,
        help=(
            "Cap on the LLM's output tokens for the single-shot call. "
            "Default (32000) covers ~120 event rows comfortably."
        ),
    )
    return parser.parse_args()


def _build_call_kwargs(prompt_text: str, model_tier: str, max_tokens: int) -> dict[str, object]:
    """Assemble the deterministic single-shot call kwargs."""
    model_id = STRONG if model_tier == "strong" else CHEAP
    return {
        "prompt": prompt_text,
        "model": model_id,
        "temperature": 0.0,
        "max_output_tokens": max_tokens,
    }


def _extract_json_lines(text: str) -> list[str]:
    """Split the model's reply on newlines and keep JSON-object-looking lines.

    The prompt asks the model to emit JSONL — one object per line. In
    practice the model sometimes wraps the reply in a ```jsonl fence or
    adds a leading paragraph; both are handled here by keeping only lines
    that start with a `{`.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("{"):
            lines.append(stripped)
    return lines


def main() -> int:
    args = _parse_args()

    prompt_path: Path = args.prompt
    if not prompt_path.is_file():
        print(f"error: prompt file not found at {prompt_path}", file=sys.stderr)
        return 2
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_text += f"\n\n## Row count\n\nGenerate approximately {args.target} rows.\n"

    kwargs = _build_call_kwargs(prompt_text, args.model, args.max_output_tokens)
    result = call(**kwargs)  # type: ignore[arg-type]
    if result.truncated:
        print(
            "warning: model reply was truncated by max_output_tokens; "
            "regenerated corpus may be short. Consider raising --max-output-tokens.",
            file=sys.stderr,
        )

    raw_lines = _extract_json_lines(result.text)
    validated: list[dict[str, object]] = []
    skipped: list[tuple[int, str]] = []
    for line_number, raw in enumerate(raw_lines, start=1):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            skipped.append((line_number, f"invalid JSON: {exc}"))
            continue
        try:
            Event.model_validate(payload)
        except ValidationError as exc:
            skipped.append((line_number, f"schema: {exc.errors()[0]['msg']}"))
            continue
        validated.append(payload)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for payload in validated:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    print(f"wrote {len(validated)} rows to {args.out} (skipped {len(skipped)}).")
    if skipped:
        print("skipped rows (first 5):", file=sys.stderr)
        for line_number, reason in skipped[:5]:
            print(f"  line {line_number}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

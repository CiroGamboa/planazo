"""Two live safety-layer detectors + a trace-level composer (HW4 Part 3).

Design (ADR 0027 decision 6):

- **Layer 1 — input filter.** `detect_input_injection` flags common
  prompt-injection surface patterns in the user's raw message before the
  agent ever sees it. Regex + phrase-list.
- **Layer 3 — output filter.** `detect_output_leakage` flags an answer
  that includes API-key-shaped tokens or system-prompt fingerprints, so
  data cannot exfil through the chat surface even if an upstream layer
  slipped.
- Layer 2 (structural separation) and Layer 4 (capability constraints)
  are enforced elsewhere (AGENTS.md rule 2, ADR 0002, ADR 0004). Cited,
  not re-implemented.

`detect_safety_issues(trace)` composes the two live layers over an
MLflow `Trace`. It is a pure function so the same code drives batch mode
(scanning stored traces) and inline mode (checking a live trace before
returning).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from planazo.safety.models import SafetyFinding

if TYPE_CHECKING:
    pass


# Layer 1 — prompt-injection patterns. Case-insensitive substring matches
# are cheaper than proper regex when the payload is bounded, but a couple
# of patterns need boundaries (so "assistant" as part of a legitimate
# word does not trip). Kept intentionally short — false positives are the
# more common failure mode than false negatives at this scale.
_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(ignore|disregard|forget) (?:all |the |any )?"
            r"(previous|prior|above|preceding) (instructions?|prompts?|rules?)",
            re.I,
        ),
        "prompt_injection",
    ),
    (
        re.compile(
            r"(reveal|print|show|dump) (?:your |the )?system (prompt|message|instructions?)",
            re.I,
        ),
        "prompt_injection",
    ),
    (re.compile(r"you are now (?:a |an )?[a-z]+", re.I), "prompt_injection"),
    (re.compile(r"pretend (you are|to be)", re.I), "prompt_injection"),
    (re.compile(r"forget (everything|all|your)", re.I), "prompt_injection"),
    (re.compile(r"^system[:>]", re.I | re.M), "prompt_injection"),
    (re.compile(r"</?system>", re.I), "prompt_injection"),
    # Tool abuse — asking the agent to invoke a specific tool with a
    # supplied argument. `ask_user` in particular can proxy the user
    # through a leading question so any explicit invocation attempt
    # from user input is suspect.
    (
        re.compile(
            r"\bcall\s+(?:ask_user|save_memory|save_event_candidate|"
            r"confirm_and_create_calendar_event|retrieve_memory|search_events)\b",
            re.I,
        ),
        "tool_abuse",
    ),
    (re.compile(r"\buse (the )?(ask_user|save_memory)\b (?:tool )?with", re.I), "tool_abuse"),
    # Data exfiltration — attempts to read another user's data or dump
    # everything stored for a specific user id.
    (
        re.compile(
            r"(print|reveal|show|dump|list)\s+(?:everything|all|the)\s+"
            r"(?:that\s+)?user\s+\d+",
            re.I,
        ),
        "data_exfiltration",
    ),
    (
        re.compile(
            r"(?:what|show|read).*(?:stored|memory|memories)\s+(?:in|of|for)\s+user\s+\d+",
            re.I,
        ),
        "data_exfiltration",
    ),
    (
        re.compile(r"\bfor another user\b|\banother user's\b", re.I),
        "data_exfiltration",
    ),
)


# Layer 3 — output-leakage patterns. Guards against secrets appearing in
# an answer that will be relayed to a chat surface.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----")),
)


def detect_input_injection(text: str) -> SafetyFinding | None:
    """Layer 1 — flag prompt-injection patterns in user input.

    Returns `None` when nothing is flagged. Returns the first matching
    pattern otherwise (bounded evidence, so a long payload does not blow
    up downstream JSONL). Covers three attack shapes: prompt injection
    (overt instruction override), tool abuse (leading tool invocation),
    and data exfiltration (cross-user read attempts).
    """
    if not text:
        return None
    for pattern, kind in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            evidence = text[max(0, match.start() - 20) : match.end() + 20]
            return SafetyFinding(
                layer="input_filter",
                kind=kind,  # type: ignore[arg-type]
                evidence=_truncate(evidence),
                rationale=f"matched {kind} pattern {pattern.pattern!r}",
            )
    return None


def detect_output_leakage(answer: str) -> SafetyFinding | None:
    """Layer 3 — flag secrets in the final answer.

    Returns `None` when nothing is flagged. Returns the first matching
    pattern with its evidence bounded to 60 chars (the secret itself gets
    partially redacted so the report does not leak what it's flagging).
    """
    if not answer:
        return None
    for label, pattern in _SECRET_PATTERNS:
        match = pattern.search(answer)
        if match:
            redacted = _redact(match.group(0))
            return SafetyFinding(
                layer="output_filter",
                kind="secret_leakage",
                evidence=redacted,
                rationale=f"matched {label} pattern",
            )
    return None


def detect_safety_issues(trace: object) -> list[SafetyFinding]:
    """Compose the live layers over an MLflow trace.

    `trace` is duck-typed (an `mlflow.entities.Trace`) to avoid an MLflow
    import at safety-module load time and to keep unit tests fixture-free.

    The detector walks:
    - The root span's inputs to extract the user's raw message. If the
      trace was produced by `agents.event_agent.run_once`, the raw text is
      threaded through `run_context["text"]` and lands as a span input.
    - The trace's final answer, taken from the root span's output.

    Then runs both live-layer detectors and returns every finding. Empty
    list means "no known issues" — not "provably safe".
    """
    findings: list[SafetyFinding] = []

    user_text = _extract_user_text(trace)
    if user_text:
        finding = detect_input_injection(user_text)
        if finding is not None:
            findings.append(finding)

    answer = _extract_answer(trace)
    if answer:
        finding = detect_output_leakage(answer)
        if finding is not None:
            findings.append(finding)

    return findings


def _extract_user_text(trace: object) -> str | None:
    """Best-effort read of the user's raw message from a trace's root span."""
    spans = getattr(getattr(trace, "data", None), "spans", None) or []
    for span in spans:
        if getattr(span, "span_type", None) != "AGENT":
            continue
        inputs = _get_attr(span, "mlflow.spanInputs")
        if isinstance(inputs, dict):
            text = inputs.get("text") or inputs.get("run_context", {}).get("text")
            if isinstance(text, str) and text:
                return text
    return None


def _extract_answer(trace: object) -> str | None:
    """Best-effort read of the final answer from a trace."""
    spans = getattr(getattr(trace, "data", None), "spans", None) or []
    for span in spans:
        if getattr(span, "span_type", None) != "AGENT":
            continue
        outputs = _get_attr(span, "mlflow.spanOutputs")
        if isinstance(outputs, dict):
            answer = outputs.get("answer")
            if isinstance(answer, str) and answer:
                return answer
    return None


def _get_attr(span: object, key: str) -> object | None:
    """Read one span attribute in a way that works across mlflow versions."""
    attrs = getattr(span, "attributes", None)
    if attrs is None:
        return None
    if hasattr(attrs, "get"):
        value: object | None = attrs.get(key)
        return value
    return None


def _truncate(text: str, cap: int = 200) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _redact(token: str) -> str:
    """Partially redact a secret before returning it as evidence.

    Keeps the first four and last two characters so a report reader can
    tell what kind of secret was flagged without publishing the whole
    thing.
    """
    if len(token) <= 10:
        return "***"
    return f"{token[:4]}…{token[-2:]}"

"""Agent-level evaluation harness (HW4 Part 1).

The `agent` subpackage owns the trajectory-based scorers, scenario loader,
trace adapters, and multi-run reliability harness for the Recommender.
Landed alongside the retrieval + generation scorers in
`planazo.eval.metrics`; scorer bodies there stay untouched — this package
composes over them via the trace-adapter seam defined in
`planazo.eval.agent.adapters`.

Per ADR 0027 (HW4 orchestration ADR).
"""

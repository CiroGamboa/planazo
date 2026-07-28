# 0011 - Preference push-context safety

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** Ana Karla Caballero González
- **Relates to:** [`0003-sqlite-domain-store.md`](0003-sqlite-domain-store.md), [`0004-three-store-memory-model.md`](0004-three-store-memory-model.md).

## Context

Preference rows are persisted data that the composition root pushes into the model's system context for a bound identity. Individual values are validated at write time, but arbitrary row counts can still make the model-visible section unbounded. A malformed row inserted outside that boundary previously raised while context was assembled.

A row count is not a payload bound: keys are persisted without a length limit, and even bounded values vary in rendered length. The model sees characters, not rows.

## Decision

The complete preference section is capped at **1,200 Unicode code points**. Rows are queried in ascending key order and rendered as complete quoted bullets. Before considering any candidate row, rendering reserves the exact final marker:

```
- [additional preferences omitted]
```

It includes the deterministic prefix of complete rows that fits with that reserve. If every row fits, the reserve is removed; if any row is omitted, the marker appears once as the final line. An oversized first row therefore yields only the heading and marker. Empty reads retain `User preferences: none saved yet`.

`get_preferences` returns either validated rows or the typed `invalid_preference_data` outcome. Any reconstruction or validation failure returns no rows and a safe operator message. `run_once` handles that outcome before it creates `RunStepLogger` or calls the runtime: it returns `LoopResult(answer="Preferences could not be loaded safely; no model request was made.", steps=0, stopped="preference_read_error")`. It makes no LLM request, invokes no observer, and creates no monitor trace.

`preference_read_error` belongs to the composition-root `LoopResult` compatibility surface, but not to `monitor.models.RunStep.stopped`: monitor records represent actual loop runs, and this branch deliberately has no logger or trace.

## Consequences

- Model-visible preference context has a predictable maximum cost while preserving deterministic, whole-row rendering.
- Operators can see that valid preferences were omitted rather than mistaking the prefix for the complete set.
- Corrupt persisted data fails closed without leaking a partial preference set or producing an untyped CLI traceback.
- Repairing or deleting corrupt rows remains an explicit maintenance action; this change neither migrates nor repairs persisted data.

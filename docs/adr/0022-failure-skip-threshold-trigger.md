# ADR 0022 — `failure_skip` as an observable threshold trigger

**Status:** Accepted
**Date:** 2026-07-29
**Related:** [ADR 0011 §D9 (failure-skip gate)](0011-scheduled-ingestion.md), [ADR 0020 (curator agent)](0020-catalog-curator-agent.md) — same notifier pattern.

## Context

The system-shape requirements ask for **two trigger types that are not user messages** — one from `{schedule, heartbeat}` and one from `{webhook, external event, threshold}`. The obvious schedule trigger is `planazo-scheduler --tick` (cron). What was missing until this ADR was an equally visible **threshold** trigger.

The scheduler already had the threshold internally: `_scheduler_gate` returns `failure_skip` when `scan_state.consecutive_failures >= CONSECUTIVE_FAILURE_SKIP_THRESHOLD` (per ADR 0011 §D9). But the trigger was purely internal — the URL was silently skipped, the counter reset, one `SchedulerRunRecord(gate_reason="failure_skip")` line landed in `var/scheduler_runs.jsonl`, and no operator learned about it unless they were actively tailing the log.

Two observed problems this leaves open:

1. **Silent alert loss.** A permanently broken Instagram account (removed handle, renamed, geo-blocked) sits in `data/sources.yaml` unnoticed, quietly consuming a `failure_skip` line every 4 tick-intervals. An operator running `--tick` under cron never sees it.
2. **Missing external-side compliance.** The system-shape requirement wants a *trigger* that fires an observable action, not just a gate that reads `False`. A silent skip meets the "silence branch" requirement (ADR 0011 §D9) but does not meet the "trigger fires" requirement.

## Decision

### D1: Wire the `failure_skip` gate to fire an admin Telegram DM

When `_process_source_url` observes `gate_reason == "failure_skip"`, it now calls
`scheduler.notifier.notify_admins_of_failure_skip(source_url, consecutive_failures)`
before the audit-log append. The notifier fans out one Telegram DM per admin listed in
`TELEGRAM_ADMIN_USER_IDS`, using the shared `TELEGRAM_BOT_TOKEN` the bot already polls with.

The trigger is a **threshold-crossing observer**: it fires when the counter first reaches
the threshold on a scheduled tick. The URL is still skipped this tick (the silence branch
of the gate — `SchedulerRunRecord.gate_reason="failure_skip"` is unchanged). What changes is
that the operator learns the threshold was crossed.

**Rejected alternative:** log a WARNING to stderr only. **Reason:** cron `--tick` output is
usually redirected to `var/scheduler.log`; a WARNING there is discoverable only under active
inspection. Telegram DM is pull-based on the operator's side (they see it whenever they
open the app), which matches the "learn about it eventually" alert cadence a permanently
broken source deserves.

**Rejected alternative:** fire the DM only once per URL until it recovers. **Reason:** would
require a new `scan_state.last_failure_skip_notified_at` column and a cooldown check. The
natural cadence of the existing gate — one skip per 4 tick-intervals for a permanently
broken URL (ADR 0011 §D9's "one attempt per two tick-intervals" pattern) — is already
alerting once per ~24 hours at the default 6h cadence. That is not spam. If it becomes so,
a follow-up ticket can add the cooldown column.

### D2: The message text stays Rule 2-safe

The DM carries **only** the URL and the integer `consecutive_failures` counter, both drawn
from `scan_state` (the scheduler's own scoped state, never from an Extractor tool result).
No Instagram caption, venue text, or LLM-produced rationale ever crosses this boundary.

**Rejected alternative:** include the last error message from `SchedulerRunRecord.errors[]`.
**Reason:** `errors[]` is regex-locked at the model boundary but is still tool-derived —
including it would widen the leak surface for no operator-actionable gain. The URL alone
tells the operator which account to inspect.

### D3: Best-effort, Rule 4-disciplined

Every network operation is wrapped in `(urllib.error.URLError, TimeoutError, OSError, ValueError)`
and swallowed to WARNING. A Telegram outage never breaks a tick. The tick's primary flow —
the empty `SchedulerRunRecord` written to `var/scheduler_runs.jsonl` and the
`scan_state.consecutive_failures` reset — commits before the notifier fires, so an admin-side
failure can never invalidate the audit trail.

Belt-and-braces `try/except` around the `notify_admins_of_failure_skip` call in
`_process_source_url` protects against a future notifier refactor that changes the
contract.

### D4: The trigger is opt-in via env vars

`TELEGRAM_BOT_TOKEN` unset or `TELEGRAM_ADMIN_USER_IDS` unset → silent no-op. A fresh deploy
of `planazo-scheduler` is safe to run without operator setup; the trigger simply doesn't fire
until an operator opts in.

## Consequences

- **Trigger inventory** for the system-shape requirement now cleanly maps: `planazo-scheduler --tick` (schedule) and this threshold-observer (threshold). Neither is a user message; both fire actions the operator can observe.
- **Silence-branch record** is preserved: the URL is still skipped this tick with `gate_reason="failure_skip"` in the JSONL audit; the DM is *additional* observation, not a replacement.
- **Alert cadence** for a permanently broken source is ~1 DM per 24h at the default 6h cadence. An operator learning about repeated failures via Telegram will typically go inspect the source and either fix `sources.yaml` or remove the entry — the alert is action-driving.
- **Trust boundary** stays intact: message content is scheduler-scoped state only. See Rule 2 §D2.

## Rejected alternatives (already covered inline in D1–D4)

## Follow-up work (deferred)

- **Cooldown column** — if the ~24h cadence proves too chatty in production, add `scan_state.last_failure_skip_notified_at` and a check against a configurable window before firing.
- **Reunion notification** — when a broken URL recovers, fire a "back to healthy" DM. Requires threshold-transition detection on the *success* side too (`consecutive_failures` returning to 0 after having been non-zero).
- **Grouping** — if many URLs cross the threshold on the same tick, batch them into one DM. Currently N URLs = N DMs per tick.

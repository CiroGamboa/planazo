# Architecture Decision Records

We use ADRs (Michael Nygard format) to record decisions that are hard to reverse — technology choices, data-contract shapes, tool boundaries, approval-gate policies, deployment shape. If a future ticket needs to understand *why* something is the way it is, that answer lives here.

## When to write one

Write an ADR when:

- A decision constrains future work across multiple tickets.
- A decision was contested — there was a real alternative, and we want future readers to know why we chose this one.
- Reversing the decision would require coordinated changes in more than one place.

Examples for this project: LLM provider choice, the agent-loop shape (hand-rolled vs framework), the tool interface & registration convention, the approval-gate contract for irreversible actions, the persistence store, the event-source integrations added or removed, the extraction-error taxonomy. The MVP itself will spawn a stack of these — see the ADR table in [`../MVP-ARCHITECTURE.md`](../MVP-ARCHITECTURE.md) for the ones already scheduled (SQLite domain store, three-store memory model, multi-agent shape, Instagram extraction, monitor scheduling, Telegram bot interface).

Do **not** write an ADR for:

- Style preferences already covered by `ruff`.
- Naming decisions inside one module.
- Anything with no alternative worth naming.

## How

1. Copy [`0000-template.md`](0000-template.md) to `NNNN-slug.md` — next unused number, kebab-case slug.
2. Fill in Status / Context / Decision / Consequences.
3. Reference the ADR from `AGENTS.md` or the relevant PR if it's load-bearing there.
4. ADRs are immutable once accepted. To change a decision, write a new ADR that supersedes the old one and mark the old one `Status: Superseded by NNNN`.

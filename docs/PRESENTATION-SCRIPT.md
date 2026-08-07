# Planazo — Presentation Speaker Script

Companion to [`docs/PRESENTATION.html`](PRESENTATION.html). One short block per slide (roughly 30–60 seconds each; total talk ≈ 10 minutes). Every results slide ends with **"What we observed"** — that's the *conclusion*, not just the number.

Suggested team split:

- **Ciro** — slides 1–6 (framing + architecture)
- **Daniel** — slides 7–11 (RAG + retrieval/generation results)
- **Ana Karla** — slides 12–16 (agent eval + safety + wrap-up)

Anyone can hand off mid-section; the script is written so each block reads standalone.

---

## Slide 1 — Title

> Hi everyone. We're Ciro, Daniel, and Ana Karla. This is Planazo — an agentic assistant that helps someone in Barcelona find events they might want to go to. Over the next ten minutes we're going to walk you through what we built, how the retrieval works, and what we actually learned from evaluating it.

*(Advance to slide 2.)*

---

## Slide 2 — What is Planazo?

> The idea is simple. Someone messages the bot in plain language — "find tech events this weekend" — and Planazo figures out what they meant, searches a catalog of Barcelona events, ranks them, and answers. The whole thing is agentic, meaning: it's not a fixed pipeline. An LLM decides what to do next at each step. Behind the scenes there's a Telegram bot, an intent interpreter, and the recommender agent, which reads from a SQLite catalog and returns a ranked answer.

---

## Slide 3 — System architecture

> Here's the whole system on one slide. Three agents in green: the Recommender talks to users, the Extractor turns raw Instagram posts into structured event rows, and the Curator runs daily to clean up the catalog. They all read and write the same SQLite events table. The user's own memory — their preferences — lives in a separate per-user store, and the committed markdown rules are always pushed into the system prompt. The two loops on the right — scheduler ingestion and daily curation — run without a user in the loop.

---

## Slide 4 — Three agents, three jobs

> Zooming into the three agents. The Recommender runs on every user turn — it's the interactive one. The Extractor runs on-demand when the scheduler discovers a new Instagram post. The Curator runs once a day and cleans up stale, duplicate, or mis-categorised events. All three are typed LangGraph state graphs, which we chose because it lets us describe agents as graphs of nodes and edges rather than one big while-loop. The typing gives us Pydantic validation at every boundary — an LLM tool call that returns the wrong shape gets rejected before it can corrupt anything.

---

## Slide 5 — Recommender tool set

> These are the six tools the Recommender exposes to the LLM. Search events is RAG-backed — we'll get to that in a minute. The four memory tools are the interesting design point: they're closures over the user's ID. Meaning the LLM literally cannot call retrieve_memory with somebody else's ID, because the ID isn't a parameter it can pass — it's baked in when the tool is registered. That eliminates a whole class of cross-user data leaks by construction, not by policy. Ask_user is our clarification tool: the model can ask one non-blocking question per turn.

---

## Slide 6 — The agentic loop

> The loop is the classic observe → reason → act → verify pattern, but each step is an explicit graph node. Observe: read the user's intent, the pushed context, their preferences. Reason: the LLM decides — do I need a tool, or can I answer? Act: if it wants a tool, LangGraph's ToolNode dispatches. Verify: the tool's return goes through Pydantic. Loop back or produce a final answer. The key thing is: the loop, the stopping conditions, and the guardrails are ours — the LLM is only inside the "reason" node, never in control of the flow.

---

## Slide 7 — RAG, what and why

> Here's why we needed RAG at all. Before RAG, we could filter events by category, city, and date. That works for "tech events this weekend". It doesn't work for "recital at Palau de la Música" — there's no "recital" category, and the user is really asking for classical music at a specific venue. RAG lets us search by meaning. The hard filters still gate the candidate set first — we're not going to return events in Madrid when someone asks about Barcelona — but *within* that filtered set, RAG ranks by semantic and lexical similarity.

---

## Slide 8 — RAG corpus and chunking

> One design choice we're proud of: one chunk per event. Most RAG tutorials talk about splitting long documents into overlapping windows, but an event is not a document — it's a row. Title, description, venue, tags, time, price are meaningful *together*. Splitting them would lose the join. The chunk text is a deterministic projection of those fields, and the chunk ID is the event ID, so we don't need any resolver mapping. Two datasets: 60 seed events in the live database for demos, and a separate 120-event curated corpus that we use only for the retrieval eval.

---

## Slide 9 — RAG search flow

> Here's the two-stage pipeline. On the left, the query enters and hits the hard filters — category, city, date, price. Then the survivors go through two rankers in parallel. Dense embeddings, using sentence-transformers, capture meaning — that's how "recital" finds "concert". BM25 captures exact terms — venue names, acronyms, proper nouns. We fuse the two rankings with Reciprocal Rank Fusion, which is a formula that rewards documents that rank high in either. That gives us 20 candidates. Those go into a cross-encoder reranker, which scores query and document together — more expensive but more accurate. Top 5 out.

---

## Slide 10 — RAG retrieval results

> These are the real numbers from our eval, 20 golden queries against the 120-event corpus, reranker on. The highlighted row is k equals 5, which is the default we ship. Hit-at-5 is 1.0 — meaning for every one of our 20 queries, at least one correct event is in the top 5. Recall-at-5 is 0.958, meaning we're pulling nearly all the golden events into that top 5. MRR is 1.0 — the *first* golden hit always lands at rank 1.
>
> **What we observed:** the reranker's contribution shows up in MRR — without it we were at 0.944, meaning about 1 in 20 queries had the golden hit at rank 2 or lower. Adding the cross-encoder pushes that to 1.0. For a user reading the top result, that's the difference between "worked" and "worked the first time".

---

## Slide 11 — RAG generation results

> Now the generation side — how good is the *answer text*, not just the retrieval. We use an LLM as a judge, scoring each answer against the ground truth on three dimensions. Faithfulness: 0.985. The model does not invent facts. Whatever it says, it stays inside the retrieved chunks. Context precision: 0.693 — reasonable, meaning most of the chunks we retrieved were actually useful. But answer relevance is 0.333.
>
> **What we observed:** the model retrieves well and doesn't hallucinate, but it summarises loosely. It'll pull the right events into context and then produce an answer that doesn't quite address the specific question the user asked. That's a prompt-engineering problem, not a retrieval problem — and it's the single most valuable signal from this whole eval. Next iteration: tighten the generation prompt to force the model to directly answer the query, not just paraphrase what it retrieved.

---

## Slide 12 — Agent evaluation, pass@3 vs pass^3

> This is the HW4 harness. Twelve scenarios covering happy paths, ambiguous requests, preferences, refusal paths, and edge cases. Each scenario runs three times. Temperature 0.7 — because at temperature zero the LLM produces the same answer three times, which tells you nothing about reliability. The two numbers we report are pass-at-3 (did it succeed *at least* once) and pass-cubed (did it succeed *all three* times).
>
> **What we observed:** only 2 out of 12 scenarios are reliably right. Two more are flaky — they pass once or twice out of three. The remaining 8 hard-fail on all three runs, with the same error type — the LLM decides not to call the search tool at all and just answers from world knowledge. That's a real product signal, not a scoring bug. Our next iteration is prompt-engineering the recommender to be more aggressive about invoking the tool. Notice how pass-at-3 would have made the picture look better — 4 out of 12 pass at least once. Pass-cubed is what tells you what a user actually experiences.

---

## Slide 13 — Tools ≠ outcome

> This slide is our favourite finding. Two scenarios. On the left, `tell-me-more-about-2`. The agent called the exact expected tool with the exact expected arguments — trajectory score is a perfect 1.0. But goal completion is 0.0. Why? Because the scenario refers to "item number two" from a previous turn, and our eval harness doesn't thread multi-turn state. So the tool was right; the answer couldn't satisfy the ask. On the right, the opposite: an ambiguous first-date request. The agent skipped the clarification tool it "should have" called — trajectory is 0.0. But the answer still worked, so goal completion is 0.767.
>
> **What we observed:** trajectory metrics measure *what the agent did*. Goal completion measures *what the user got*. These two disagree far more often than we expected, and neither one alone is sufficient. Judging an agent only on tool trajectory rewards it for going through the motions; judging only on goal completion misses subtle failures like calling a destructive tool that happened to produce a good-looking answer. You need both.

---

## Slide 14 — MLflow tracing

> Everything you've seen so far — the eval, the safety scan — reads from the same source: MLflow traces. Every recommender turn produces a tree of spans like this: root span, LangGraph orchestration, chat model calls with model name and latency, tool calls, and the retrieval span with the ranked event IDs. We instrument with two lines: a decorator on `run_once` and a decorator on `search_events_rag`. Everything else — the LLM and tool spans — comes free from LangChain's autolog hook. The tag chips at the bottom are how we filter later: request origin separates production traffic from eval; eval case ID is the join key back to the scenario; agent kind separates the three agents.

---

## Slide 15 — Safety hardening

> Four attack shapes, four defense layers. Direct injection — someone types "ignore previous instructions". Tool abuse — someone tries to hijack a legitimate tool. Exfiltration — someone asks to read another user's memory. Indirect injection — a poisoned event description tries to instruct the model. Three of these are caught by an input filter (Layer 1) — small regex ruleset. The fourth is caught by the architecture itself: retrieved content never enters the system role, so an injection payload inside a scraped caption is just data, not an instruction.
>
> **What we observed:** every declared attack is caught. Zero false positives on 36 legitimate scenarios. But the more interesting result is that defense in depth actually pays off — no single layer catches all four attacks. Layer 1 covers the overt cases; Layer 2 covers the sneaky ones. Layers 3 and 4 sit behind that, catching secrets in the output and blocking capability misuse. Any one of them alone would leave holes. The composition is what's safe.

---

## Slide 16 — Recap

> To close: three agents on LangGraph state graphs. RAG as a hybrid retriever plus a cross-encoder reranker. Evaluation stack that measures both trajectory and outcome, and exposes flakiness that a temperature-zero run would hide. Safety detector composed with the architecture itself. Everything traceable through MLflow, and everything reproducible with the four commands at the bottom of the slide.
>
> **The three things we'd take forward if we had another sprint:** first, tighten the generation prompt to lift answer relevance — that's the biggest headroom. Second, prompt-engineer the recommender to invoke tools more reliably — the 8-out-of-12 hard-fail pattern is the number one thing to improve. Third, wire the real Google Calendar API on top of the reference stub, so the "action" side of the agent finally has a real external surface.
>
> Thank you. Questions?

---

## Speaking tips (not for reading aloud)

- **When you say a number, name what changes because of it.** "Hit-at-5 is 1.0" is a number. "The user always finds a match in the first five results" is a conclusion. The script above does this on every results slide — do it live too.
- **On slide 12 (pass^3 numbers), don't apologise for the 8 hard-fails.** They're the most valuable output of the eval. Frame them as "the eval is doing its job — this is what we'd fix next."
- **On slide 13 (tools ≠ outcome), pause after the two numbers.** Let the audience see the contradiction before you explain it.
- **If you run over time, cut slides 4 and 8** — the three-agent card and the corpus/chunking slide. The story still works with 14 slides.
- **If someone asks about the calendar** — say it's the next feature: the tools that draft an event are already there, the API wiring is the missing piece.

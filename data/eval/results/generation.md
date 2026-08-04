# Generation eval — RAG-backed `search_events`

- Seed corpus: `data/eval/events_seed.jsonl` (120 events)
- Golden cases: `data/eval/questions.jsonl` (20 cases)
- Rerank sweep: ['on', 'off']; rerank-off subset size: 10
- Judge: `OpenCodeJudge` (model=CHEAP role, enabled=True)
- Cache root: `var/eval/judge_cache`

## Overall averages

| rerank | faithfulness | answer_relevance | context_precision |
| --- | --- | --- | --- |
| on | 0.985 | 0.333 | 0.693 |
| off | 1.000 | 0.415 | 0.580 |

## Per failure_category

| failure_category | rerank | faithfulness | answer_relevance | context_precision |
| --- | --- | --- | --- | --- |
| acronym | on | 1.000 | 0.075 | 0.700 |
| acronym | off | 1.000 | 0.000 | 0.600 |
| exact_term | on | 0.940 | 0.410 | 0.840 |
| exact_term | off | 1.000 | 0.800 | 0.800 |
| lexical_semantic_mismatch | on | 1.000 | 0.562 | 0.850 |
| lexical_semantic_mismatch | off | 1.000 | 0.550 | 0.600 |
| multi_hop | on | 1.000 | 0.475 | 1.000 |
| multi_hop | off | 1.000 | 0.550 | 0.700 |
| near_duplicate_noise | on | 1.000 | 0.367 | 0.400 |
| near_duplicate_noise | off | 1.000 | 0.350 | 0.400 |
| out_of_corpus | on | 1.000 | 0.000 | 0.125 |
| out_of_corpus | off | 1.000 | 0.000 | 0.000 |

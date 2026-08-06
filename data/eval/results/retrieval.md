# Retrieval eval — RAG-backed `search_events`

- Seed corpus: `data/eval/events_seed.jsonl` (120 events)
- Golden cases: `data/eval/questions.jsonl` (20 cases)
- Empty-golden (`out_of_corpus`) cases excluded from means: **2**
- k sweep: [1, 3, 5, 10]; rerank sweep: ['on', 'off']

## Overall averages

| k | rerank | hit_at_k | precision_at_k | recall_at_k | mrr | ndcg_at_k |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | on | 1.000 | 1.000 | 0.690 | 1.000 | 1.000 |
| 1 | off | 0.889 | 0.889 | 0.620 | 0.944 | 0.889 |
| 3 | on | 1.000 | 0.556 | 0.875 | 1.000 | 0.967 |
| 3 | off | 1.000 | 0.519 | 0.847 | 0.944 | 0.911 |
| 5 | on | 1.000 | 0.411 | 0.958 | 1.000 | 0.974 |
| 5 | off | 1.000 | 0.400 | 0.940 | 0.944 | 0.931 |
| 10 | on | 1.000 | 0.228 | 1.000 | 1.000 | 0.988 |
| 10 | off | 1.000 | 0.222 | 0.986 | 0.944 | 0.944 |

## Per failure_category

| failure_category | k | rerank | hit_at_k | precision_at_k | recall_at_k | mrr | ndcg_at_k |
| --- | --- | --- | --- | --- | --- | --- | --- |
| acronym | 1 | on | 1.000 | 1.000 | 0.875 | 1.000 | 1.000 |
| acronym | 1 | off | 1.000 | 1.000 | 0.875 | 1.000 | 1.000 |
| acronym | 3 | on | 1.000 | 0.417 | 1.000 | 1.000 | 1.000 |
| acronym | 3 | off | 1.000 | 0.417 | 1.000 | 1.000 | 1.000 |
| acronym | 5 | on | 1.000 | 0.250 | 1.000 | 1.000 | 1.000 |
| acronym | 5 | off | 1.000 | 0.250 | 1.000 | 1.000 | 1.000 |
| acronym | 10 | on | 1.000 | 0.125 | 1.000 | 1.000 | 1.000 |
| acronym | 10 | off | 1.000 | 0.125 | 1.000 | 1.000 | 1.000 |
| exact_term | 1 | on | 1.000 | 1.000 | 0.833 | 1.000 | 1.000 |
| exact_term | 1 | off | 1.000 | 1.000 | 0.833 | 1.000 | 1.000 |
| exact_term | 3 | on | 1.000 | 0.467 | 0.900 | 1.000 | 1.000 |
| exact_term | 3 | off | 1.000 | 0.467 | 0.900 | 1.000 | 1.000 |
| exact_term | 5 | on | 1.000 | 0.360 | 0.967 | 1.000 | 1.000 |
| exact_term | 5 | off | 1.000 | 0.360 | 0.967 | 1.000 | 1.000 |
| exact_term | 10 | on | 1.000 | 0.200 | 1.000 | 1.000 | 1.000 |
| exact_term | 10 | off | 1.000 | 0.200 | 1.000 | 1.000 | 1.000 |
| lexical_semantic_mismatch | 1 | on | 1.000 | 1.000 | 0.604 | 1.000 | 1.000 |
| lexical_semantic_mismatch | 1 | off | 0.500 | 0.500 | 0.292 | 0.750 | 0.500 |
| lexical_semantic_mismatch | 3 | on | 1.000 | 0.583 | 0.750 | 1.000 | 0.926 |
| lexical_semantic_mismatch | 3 | off | 1.000 | 0.500 | 0.688 | 0.750 | 0.732 |
| lexical_semantic_mismatch | 5 | on | 1.000 | 0.500 | 0.917 | 1.000 | 0.943 |
| lexical_semantic_mismatch | 5 | off | 1.000 | 0.500 | 0.896 | 0.750 | 0.799 |
| lexical_semantic_mismatch | 10 | on | 1.000 | 0.300 | 1.000 | 1.000 | 0.972 |
| lexical_semantic_mismatch | 10 | off | 1.000 | 0.300 | 1.000 | 0.750 | 0.832 |
| multi_hop | 1 | on | 1.000 | 1.000 | 0.250 | 1.000 | 1.000 |
| multi_hop | 1 | off | 1.000 | 1.000 | 0.250 | 1.000 | 1.000 |
| multi_hop | 3 | on | 1.000 | 0.833 | 0.625 | 1.000 | 0.852 |
| multi_hop | 3 | off | 1.000 | 0.667 | 0.500 | 1.000 | 0.735 |
| multi_hop | 5 | on | 1.000 | 0.700 | 0.875 | 1.000 | 0.877 |
| multi_hop | 5 | off | 1.000 | 0.600 | 0.750 | 1.000 | 0.779 |
| multi_hop | 10 | on | 1.000 | 0.400 | 1.000 | 1.000 | 0.946 |
| multi_hop | 10 | off | 1.000 | 0.350 | 0.875 | 1.000 | 0.836 |
| near_duplicate_noise | 1 | on | 1.000 | 1.000 | 0.611 | 1.000 | 1.000 |
| near_duplicate_noise | 1 | off | 1.000 | 1.000 | 0.611 | 1.000 | 1.000 |
| near_duplicate_noise | 3 | on | 1.000 | 0.667 | 1.000 | 1.000 | 1.000 |
| near_duplicate_noise | 3 | off | 1.000 | 0.667 | 1.000 | 1.000 | 1.000 |
| near_duplicate_noise | 5 | on | 1.000 | 0.400 | 1.000 | 1.000 | 1.000 |
| near_duplicate_noise | 5 | off | 1.000 | 0.400 | 1.000 | 1.000 | 1.000 |
| near_duplicate_noise | 10 | on | 1.000 | 0.200 | 1.000 | 1.000 | 1.000 |
| near_duplicate_noise | 10 | off | 1.000 | 0.200 | 1.000 | 1.000 | 1.000 |
| out_of_corpus | 1 | on | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 1 | off | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 3 | on | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 3 | off | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 5 | on | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 5 | off | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 10 | on | n/a | n/a | n/a | n/a | n/a |
| out_of_corpus | 10 | off | n/a | n/a | n/a | n/a | n/a |

# Rerank-off subset

Selected 10 of 10 target cases via a deterministic balanced-across-failure-categories algorithm (`select_rerank_off_subset`). The rerank-off numbers in `generation.md` are the mean over exactly these case ids.

| case_id | failure_category | query |
| --- | --- | --- |
| q001 | exact_term | traditional flamenco show in the Gothic Quarter tonight |
| q002 | exact_term | techno night at Sala Apolo |
| q006 | acronym | OBC concert |
| q007 | acronym | FIB pre-party |
| q010 | lexical_semantic_mismatch | cheap flamenco |
| q011 | lexical_semantic_mismatch | budget jazz night |
| q014 | near_duplicate_noise | concerts at Sala Apolo the same night |
| q017 | multi_hop | free tech meetup in Eixample |
| q018 | multi_hop | outdoor sports that are free |
| q019 | out_of_corpus | opera in Milan tomorrow |

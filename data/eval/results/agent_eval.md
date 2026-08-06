# Agent eval — HW4 Part 1 (Recommender)

- Scenarios evaluated: 12
- Runs per scenario: 3
- Temperature: 0.7
- pass threshold (tool_selection): 0.5

| case_id | pass@3 | pass^3 | avg_tool_selection | avg_traj_precision | avg_traj_recall |
| --- | --- | --- | --- | --- | --- |
| cheap-tech-weekend | yes | yes | 1.000 | 0.500 | 1.000 |
| musica-fin-semana-es | yes | no | 0.667 | 0.333 | 0.667 |
| palau-musica-recital | yes | no | 0.667 | 0.333 | 0.667 |
| respect-no-metal-preference | yes | no | 0.333 | 0.333 | 0.333 |
| free-events-tonight | yes | no | 0.333 | 0.167 | 0.333 |
| within-2km-of-poblenou | yes | yes | 1.000 | 1.000 | 1.000 |
| first-date-ambiguous | yes | no | 0.333 | 0.333 | 0.333 |
| events-in-madrid | no | no | 0.000 | 0.000 | 0.000 |
| tell-me-more-about-2 | yes | yes | 1.000 | 0.667 | 0.667 |
| save-i-love-jazz | no | no | 0.000 | 0.000 | 0.000 |
| create-event-without-approval | no | no | 0.000 | 0.000 | 0.000 |
| next-30-days-food | no | no | 0.000 | 0.000 | 0.000 |

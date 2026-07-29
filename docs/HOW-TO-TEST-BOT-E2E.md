# How to test the bot end-to-end

Copy-paste-ready sequence for a colleague to smoke the full stack: seed the
database, extract real Instagram events, run the recommender CLI, and drive
the Telegram bot.

## 1. Environment

Requires: `uv`, a Telegram bot token from @BotFather, an OpenCode API key.

```bash
export OPENCODE_API_KEY=<your-key>
export TELEGRAM_BOT_TOKEN=<your-bot-token>
# Optional — needed only for --backend hikerapi flows below:
export PLANAZO_IG_HIKER_API_KEY_1=<your-hikerapi-key>
```

## 2. Fresh database with seed events

```bash
rm -f var/planazo.db
uv run python -c "from planazo.storage import db; db.connect().close()"
uv run python scripts/seed_events.py
```

Expected: `Inserted 25 events, skipped 0 (already present).`

## 3. Optional — extract events from Instagram

Single post (fastest, ~$0.05):

```bash
uv run planazo-scheduler --once https://www.instagram.com/p/<SHORTCODE>/ --verbose
```

Or scan the last N posts of any account (no `sources.yaml` edit needed):

```bash
# Anonymous backend (free, works for creator accounts)
uv run planazo-scheduler --scan-account https://www.instagram.com/<username>/ --limit 5 --verbose

# HikerAPI backend (needed for business accounts; requires env vars above)
uv run planazo-scheduler --scan-account https://www.instagram.com/<username>/ --limit 5 --backend hikerapi --verbose
```

Verify new rows landed:

```bash
sqlite3 var/planazo.db "SELECT COUNT(*), source FROM events GROUP BY source;"
```

## 4. Recommender via CLI (no Telegram)

```bash
uv run planazo-agent "what can I do this weekend" --user-id 1
```

Expected: 3 ranked candidates. Then verify persistence:

```bash
sqlite3 var/planazo.db "SELECT rank_position, event_id FROM recommendations ORDER BY id DESC LIMIT 3;"
sqlite3 var/planazo.db "SELECT run_id, user_query FROM agent_runs ORDER BY id DESC LIMIT 3;"
```

## 5. Bot on Telegram

```bash
uv run python -m planazo.bot
```

Leave the bot running. In your Telegram chat with the bot:

| Message                       | Expected                                              |
| ----------------------------- | ----------------------------------------------------- |
| `/start`                      | Welcome message + list of commands.                   |
| `/help`                       | List of commands.                                     |
| `/me`                         | Your internal `user_id` + handle + count of prefs.    |
| `/prefs`                      | Empty (first time) or list of stored preferences.     |
| `/prefs set city Barcelona`   | Confirmation the row was saved.                       |
| `/prefs`                      | Now shows the row.                                    |
| `/find music this weekend`    | Either 3 ranked candidates OR a clarification prompt. |
| (answer the clarification)    | Results, and the answer is silently saved as a pref.  |
| `more results`                | Next 3 candidates; prior ones excluded.               |
| `tell me about 2`             | Detail card for candidate #2 from the last batch.     |
| `/prefs remove city`          | Confirmation the row was removed.                     |
| Random text (`"hi"`)          | Silence (intentional — bot only responds mid-flow).   |

## 6. Verify persistence between messages

In a second terminal while the bot is running:

```bash
sqlite3 var/planazo.db "SELECT user_id, key, value FROM preferences;"
sqlite3 var/planazo.db "SELECT run_id, rank_position, event_id FROM recommendations ORDER BY id DESC LIMIT 10;"
sqlite3 var/planazo.db "SELECT run_id, user_query, answer_text FROM agent_runs ORDER BY id DESC LIMIT 5;"
sqlite3 var/planazo.db "SELECT user_id, pending_clarification FROM conversation_state;"
```

## 7. Optional — out-of-band monitor grading

Grade recent runs against a categorical rubric:

```bash
uv run planazo-monitor --since 24h
```

Expected: `graded reports written: data/monitor/<YYYY-MM-DD>.md`

Or grade the deterministic seed runs (no live data needed):

```bash
uv run planazo-monitor --dry-run --out /tmp/monitor-test
```

## Troubleshooting

- **`sqlite3.OperationalError: no such table: events`** — you skipped step 2. Delete `var/planazo.db` and re-run steps 2-3.
- **`TELEGRAM_BOT_TOKEN missing`** — check `env | grep TELEGRAM`. Reload your shell.
- **Bot doesn't reply** — check the bot process is still polling (step 5 command still running).
- **`/find` returns "no results"** — the seed events span the 30 days after the seed script ran. Re-run `scripts/seed_events.py` if the dates drifted into the past.
- **HikerAPI 401/403 during `--scan-account`** — key retired temporarily. Wait 5 minutes or add another `PLANAZO_IG_HIKER_API_KEY_2` env var to the pool.

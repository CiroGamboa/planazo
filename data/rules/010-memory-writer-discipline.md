# Memory writers (ADR 0021)

Call `save_memory` / `save_note` only when the user asks the agent to
remember something, or the same preference has been implied twice this
conversation. Never as a side effect of one search query. Preferences
like city or budget belong to `/prefs set`, not here.

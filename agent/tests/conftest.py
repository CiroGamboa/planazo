import os

# agentlib.core reads this at import time; set a dummy value so test collection
# doesn't require a real .env. No test in this suite makes a real network call.
os.environ.setdefault("OPENCODE_API_KEY", "test-key-not-real")

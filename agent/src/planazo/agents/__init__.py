"""Planazo's event-discovery agent.

`loop.py` holds the generic observe -> reason -> act -> verify tool-calling
loop; `event_agent.py` binds it to the event-discovery tools in
`tools.tools` and exposes `run_once()`; `cli.py` is the `planazo-agent`
console entrypoint.
"""

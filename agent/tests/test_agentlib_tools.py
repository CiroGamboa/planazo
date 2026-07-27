from agentlib import core, tools


def test_tools_reexports_core_symbols() -> None:
    assert tools.call is core.call
    assert tools.show is core.show
    assert tools.CHEAP == core.CHEAP
    assert tools.STRONG == core.STRONG
    assert tools.MODELS == core.MODELS

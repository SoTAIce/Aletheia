"""Tools package with lazy loading of the existing concrete tool exports."""

from typing import Any

__all__ = ["DEFAULT_LOCAL_AGENT_TOOLS", "retrieve_knowledge", "get_current_time"]


def __getattr__(name: str) -> Any:
    if name == "retrieve_knowledge":
        from app.tools.knowledge_tool import retrieve_knowledge
        globals()[name] = retrieve_knowledge
        return retrieve_knowledge
    if name == "get_current_time":
        from app.tools.time_tool import get_current_time
        globals()[name] = get_current_time
        return get_current_time
    if name == "DEFAULT_LOCAL_AGENT_TOOLS":
        tools = (__getattr__("retrieve_knowledge"), __getattr__("get_current_time"))
        globals()[name] = tools
        return tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

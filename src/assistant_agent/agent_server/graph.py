"""Deployable Assistant Graph symbol loaded by Agent Server.

This bootstrap deliberately binds neither a checkpointer nor a Store. Agent
Server injects both resources. Worker dependency hydration replaces the
disabled memory bundle in the next migration task.
"""

from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph


assistant_graph = build_assistant_loop_graph(
    checkpointer=None,
    graph_name="AssistantAgentServerGraph",
)


__all__ = ["assistant_graph"]

"""Stable compiled application for the assistant turn graph."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph


@dataclass(frozen=True)
class GraphExecutionIdentity:
    """LangGraph execution identity for one assistant conversation turn."""

    thread_id: str
    checkpoint_ns: str
    run_id: str

    @classmethod
    def for_assistant_turn(
        cls,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> "GraphExecutionIdentity":
        raw = json.dumps(
            ["assistant", agent_id, user_id, session_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()[:32]
        return cls(
            thread_id=f"assistant:{digest}",
            checkpoint_ns=f"turn:{run_id}",
            run_id=run_id,
        )

    def runnable_config(self) -> dict[str, dict[str, str]]:
        return {
            "configurable": {
                "thread_id": self.thread_id,
                "checkpoint_ns": self.checkpoint_ns,
                "run_id": self.run_id,
            }
        }


class AssistantTurnGraphApp:
    """Own the one compiled assistant graph shared by a runtime instance."""

    def __init__(self, *, checkpointer: Any | None) -> None:
        self._graph = build_assistant_loop_graph(checkpointer=checkpointer)

    @property
    def graph(self) -> Any:
        """Return the compiled graph without allowing replacement."""

        return self._graph

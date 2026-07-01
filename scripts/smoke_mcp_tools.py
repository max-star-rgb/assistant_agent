"""Offline smoke test for the Phase 5J MCP skeleton."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.mcp.server import OfflineMCPServer


def main() -> int:
    server = OfflineMCPServer()
    results = {
        "tool_list": server.call_tool("tool_list").model_dump(mode="json"),
        "agent_run": server.call_tool(
            "agent_run",
            {"user_id": "smoke_user", "session_id": "smoke_session", "text": "帮我写一段商品介绍"},
        ).model_dump(mode="json"),
        "tool_run": server.call_tool(
            "tool_run",
            {"tool_name": "product_search", "input": {"query": "白色运动鞋"}},
        ).model_dump(mode="json"),
        "redaction": server.call_tool(
            "missing_tool",
            {"api_key": "sk-test-secret", "Authorization": "Bearer token"},
        ).model_dump(mode="json"),
    }
    ok = (
        results["tool_list"]["status"] == "succeeded"
        and results["agent_run"]["status"] == "succeeded"
        and results["tool_run"]["status"] == "succeeded"
        and results["redaction"]["status"] == "failed"
        and "sk-test-secret" not in json.dumps(results, ensure_ascii=False)
        and "Bearer token" not in json.dumps(results, ensure_ascii=False)
    )
    payload = {"ok": ok, "offline": True, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

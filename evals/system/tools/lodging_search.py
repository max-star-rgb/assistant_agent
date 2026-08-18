"""PyCharm-runnable fixed-input smoke for lodging_search."""

from datetime import date

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.lodging.tool import (
    create_lodging_search_tool,
)


FIXED_INPUT = {
    "destination": "杭州",
    "check_in": date(2030, 1, 15),
    "check_out": date(2030, 1, 16),
    "adults": 1,
    "rooms": 1,
}


if __name__ == "__main__":
    raise SystemExit(run_tool_smoke(create_lodging_search_tool(), FIXED_INPUT))

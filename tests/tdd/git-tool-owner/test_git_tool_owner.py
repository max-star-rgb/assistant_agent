from assistant_agent.native_agent import assistant_agent
from assistant_agent.tools import git as git_tool


def test_git_tool_is_owned_by_tools_and_used_by_native_agent() -> None:
    assert assistant_agent.GitToolMiddleware is git_tool.GitToolMiddleware
    assert assistant_agent.GIT_TOOL_NAME == "git"
    assert git_tool.is_git_shell_command("pwd && git status") is True
    assert git_tool.is_git_shell_command("printf git") is False

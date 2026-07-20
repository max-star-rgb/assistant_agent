from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.python_interpreter import PYTHON_INTERPRETER_ENABLED_ENV
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.python_interpreter_tool import PythonInterpreterTool
from assistant_agent.tools.registry import create_default_registry


def _validate_python_interpreter(tool_input: dict[str, object]):
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="用 Python 算一下这组数的均值",
    )
    return ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="python_interpreter",
            tool_input=tool_input,
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )


def test_default_registry_declares_python_interpreter_disabled_and_redacted() -> None:
    registry = create_default_registry()

    assert "python_interpreter" in registry.list()
    spec = next(spec for spec in registry.list_specs() if spec.name == "python_interpreter")
    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert spec.required_inputs == ["code"]
    assert view.side_effect_level == "local_read"
    assert view.requires_confirmation is False
    assert view.enabled_by_default is False
    assert view.requires_env == [PYTHON_INTERPRETER_ENABLED_ENV]
    assert view.redact_in_trace is True
    assert view.concurrency_group == "python_interpreter"
    assert view.max_result_chars == 6000


def test_python_interpreter_is_not_qualified_without_enable_env(monkeypatch) -> None:
    monkeypatch.delenv(PYTHON_INTERPRETER_ENABLED_ENV, raising=False)
    registry = create_default_registry()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="用 Python 分析",
        metadata={"tool_visibility": {"enabled_tools": ["python_interpreter"]}},
    )

    selection = select_prompt_tool_specs(request, registry.list_specs())

    assert "python_interpreter" not in selection.run_tool_set.qualified_tool_names
    assert selection.run_tool_set.excluded_reasons["python_interpreter"] == [
        f"missing_required_env:{PYTHON_INTERPRETER_ENABLED_ENV}"
    ]


def test_python_interpreter_is_qualified_with_env_and_explicit_visibility(monkeypatch) -> None:
    monkeypatch.setenv(PYTHON_INTERPRETER_ENABLED_ENV, "1")
    registry = create_default_registry()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="用 Python 分析",
        metadata={"tool_visibility": {"enabled_tools": ["python_interpreter"]}},
    )

    selection = select_prompt_tool_specs(request, registry.list_specs())

    assert "python_interpreter" in selection.run_tool_set.qualified_tool_names
    assert "python_interpreter" in selection.run_tool_set.executable_tool_names
    assert "python_interpreter" in [spec.name for spec in selection.prompt_tool_specs]
    assert "python_interpreter" not in selection.run_tool_set.excluded_reasons


def test_python_interpreter_tool_returns_disabled_without_enable_env(monkeypatch) -> None:
    monkeypatch.delenv(PYTHON_INTERPRETER_ENABLED_ENV, raising=False)

    result = PythonInterpreterTool().run({"code": "result = 1 + 1"})

    assert result.success is False
    assert result.error == "python_interpreter_disabled: Python interpreter tool is not enabled."
    assert result.contract is not None
    assert result.contract.status == "failed"
    assert result.model_observation == {
        "status": "rejected",
        "summary": "Python interpreter tool is not enabled.",
        "errors": [
            {
                "code": "python_interpreter_disabled",
                "message": "Python interpreter tool is not enabled.",
            }
        ],
    }


def test_action_validator_rejects_empty_python_code() -> None:
    validation = _validate_python_interpreter({"code": "   "})

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert validation.message == "python_interpreter requires code."


def test_action_validator_rejects_obvious_unsafe_python_code() -> None:
    validation = _validate_python_interpreter({"code": "import socket\nresult = 1"})

    assert validation.accepted is False
    assert validation.code == "unsafe_tool_input"
    assert (
        validation.message
        == "python_interpreter does not allow shell, network, file, process, or introspection access."
    )


def test_python_interpreter_runs_math_and_returns_observation(monkeypatch) -> None:
    monkeypatch.setenv(PYTHON_INTERPRETER_ENABLED_ENV, "1")

    result = PythonInterpreterTool().run(
        {
            "purpose": "math_analysis",
            "input_data": {"values": [2, 4, 6, 8]},
            "code": (
                "import statistics\n"
                "values = input_data['values']\n"
                "result = {'count': len(values), 'mean': statistics.mean(values)}\n"
                "print('computed mean')\n"
            ),
        }
    )
    observation = observation_from_tool_result(result)

    assert result.success is True
    assert result.data["status"] == "succeeded"
    assert result.data["stdout"] == "computed mean\n"
    assert result.data["result_json"] == {"count": 4, "mean": 5}
    assert result.contract is not None
    assert result.contract.capability == "python_interpreter"
    assert observation.status == "succeeded"
    assert observation.summary == "Python analysis succeeded."
    assert observation.structured_output["result"] == {"count": 4, "mean": 5}
    assert "statistics" not in str(observation)


def test_python_interpreter_supports_code_analysis_with_ast(monkeypatch) -> None:
    monkeypatch.setenv(PYTHON_INTERPRETER_ENABLED_ENV, "1")

    result = PythonInterpreterTool().run(
        {
            "purpose": "code_analysis",
            "input_data": {"source": "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"},
            "code": (
                "import ast\n"
                "tree = ast.parse(input_data['source'])\n"
                "result = {\n"
                "    'functions': [\n"
                "        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)\n"
                "    ]\n"
                "}\n"
            ),
        }
    )

    assert result.success is True
    assert result.data["result_json"] == {"functions": ["alpha", "beta"]}


def test_python_interpreter_truncates_large_stdout(monkeypatch) -> None:
    monkeypatch.setenv(PYTHON_INTERPRETER_ENABLED_ENV, "1")

    result = PythonInterpreterTool().run(
        {"code": "print('x' * 8000)\nresult = 'ok'", "timeout_s": 2}
    )

    assert result.success is True
    assert result.data["truncated"] is True
    assert len(result.data["stdout"]) == 4000
    assert result.model_observation["truncated"] is True
    assert result.model_observation["stdout_chars"] == 4000


def test_python_interpreter_times_out(monkeypatch) -> None:
    monkeypatch.setenv(PYTHON_INTERPRETER_ENABLED_ENV, "1")

    result = PythonInterpreterTool().run({"code": "while True:\n    pass", "timeout_s": 1})

    assert result.success is False
    assert result.data["status"] == "timeout"
    assert result.data["timed_out"] is True
    assert result.error == "python_execution_timeout: Python execution timed out."

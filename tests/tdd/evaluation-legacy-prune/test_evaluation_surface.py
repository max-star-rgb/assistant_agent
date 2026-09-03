from importlib.util import find_spec


def test_evaluation_package_only_keeps_native_graph_target() -> None:
    assert find_spec("assistant_agent.evaluation.native_graph_target") is not None
    assert find_spec("assistant_agent.evaluation.constants") is None
    assert find_spec("assistant_agent.evaluation.runtime_regression_contract") is None

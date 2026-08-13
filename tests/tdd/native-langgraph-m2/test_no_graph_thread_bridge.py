from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _module(relative_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def _class(module: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _methods(class_node: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _asyncio_to_thread_calls(node: ast.AST) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "asyncio"
        and child.func.attr == "to_thread"
    ]


def test_runtime_has_native_state_stream_and_no_sync_graph_stream_bridge() -> None:
    """Restoring run_stream -> to_thread(run_state) must fail the architecture gate."""

    runtime = _class(
        _module("src/assistant_agent/runtime/runtime.py"),
        "AgentGraphRuntime",
    )
    methods = _methods(runtime)

    assert "run_stream" not in methods
    assert "astream_state" in methods
    assert not _asyncio_to_thread_calls(methods["astream_state"])


def test_gateway_adapter_accepts_only_async_request_streams() -> None:
    """Restoring the sync request injection or its worker bridge must fail."""

    module = _module("src/assistant_agent/gateway/runtime_adapter.py")
    adapter = _class(module, "GatewayRuntimeAdapter")
    methods = _methods(adapter)
    init_args = {
        argument.arg
        for argument in [
            *methods["__init__"].args.args,
            *methods["__init__"].args.kwonlyargs,
        ]
    }
    top_level_functions = {
        node.name for node in module.body if isinstance(node, ast.FunctionDef)
    }

    assert "run_request" not in init_args
    assert "_sync_run_request_stream" not in top_level_functions
    assert not _asyncio_to_thread_calls(module)


def test_cross_thread_event_delivery_and_pool_checkout_bridges_remain_scoped() -> None:
    """Deleting the supported leaf-resource bridges with the graph bridge must fail."""

    event_stream = _module("src/assistant_agent/runtime/event_stream.py")
    stream = _class(event_stream, "AgentRunStream")
    publish = _methods(stream)["_publish"]
    thread_safe_calls = [
        child
        for child in ast.walk(publish)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "call_soon_threadsafe"
    ]

    pool = _class(
        _module("src/assistant_agent/gateway/runtime_pool.py"),
        "GatewayRuntimePool",
    )
    pool_stream = _methods(pool)["run_request_stream"]
    executor_calls = [
        child
        for child in ast.walk(pool_stream)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "run_in_executor"
    ]

    assert thread_safe_calls
    assert len(executor_calls) == 2

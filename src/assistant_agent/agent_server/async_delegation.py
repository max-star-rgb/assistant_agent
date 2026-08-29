"""Official async-subagent middleware for the Agent Server supervisor graphs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from deepagents.middleware import AsyncSubAgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph_sdk import get_client, get_sync_client

from assistant_agent.agent_server.auth import _internal_worker_headers
from assistant_agent.agent_server.config import WORKER_GRAPH_ID
from assistant_agent.native_agent.context import (
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
    authenticated_user_identity,
)
from assistant_agent.native_agent.state import AssistantAsyncTaskState
from assistant_agent.native_agent.tool_profiles import ToolProfile


BACKGROUND_AGENT_NAME = "general-purpose-background"
ASYNC_TASK_TOOL_NAMES = (
    "start_async_task",
    "check_async_task",
    "update_async_task",
    "cancel_async_task",
    "list_async_tasks",
)
ASYNC_TASK_AUTO_APPROVED_TOOL_NAMES = frozenset(
    {"check_async_task", "list_async_tasks"}
)
_ASYNC_TASK_TOOL_DESCRIPTIONS = {
    "start_async_task": f"""启动异步子 Agent，在独立 thread 中后台执行任务，并立即返回 task_id。

可用的 subagent_type：
- {BACKGROUND_AGENT_NAME}：后台通用只读执行 Agent，适合耗时、可并行且当前回复无需等待结果的任务。

description 必须包含完整上下文、具体目标和期望输出。启动后向用户报告 task_id 并结束当前回复；不要立即调用
check_async_task。多个互不依赖的任务可以并行启动。""",
    "check_async_task": """查询异步任务的实时 status，并在任务完成时返回 result。

task_id 必须原样使用 start_async_task 返回的值。历史 Tool 结果中的 status 可能已经过期；只有用户要求状态或结果时
才调用本 Tool。""",
    "update_async_task": """向异步任务发送补充指令 message。

task_id 必须原样使用 start_async_task 返回的值。调用会中断当前 run，并在同一 thread 上启动新 run；子 Agent
仍能看到该 thread 的既有对话，task_id 保持不变。""",
    "cancel_async_task": """取消仍在运行且已不再需要的异步任务。

task_id 必须原样使用 start_async_task 返回的值。""",
    "list_async_tasks": """列出当前会话跟踪的异步任务及其实时 status。

status_filter 可使用 running、success、error、cancelled 或 all，省略时等同 all。需要某个已完成任务的完整 result
时，使用 check_async_task。历史 Tool 结果中的 status 可能已经过期。""",
}


def async_task_tool_profile() -> ToolProfile:
    """Return the profile that progressively exposes async task lifecycle tools."""

    return ToolProfile(
        profile_id="async-tasks",
        description="启动、查询、更新、取消和列出异步子Agent任务。",
        tool_names=ASYNC_TASK_TOOL_NAMES,
    )


def build_async_subagent_middleware() -> AsyncSubAgentMiddleware:
    """Expose the upstream background-task contract for the shared worker graph."""

    middleware = AsyncSubAgentMiddleware(
        async_subagents=[
            {
                "name": BACKGROUND_AGENT_NAME,
                "description": (
                    "后台通用只读执行 Agent；适合耗时、可并行且不需要当前回复等待结果的任务。"
                ),
                "graph_id": WORKER_GRAPH_ID,
            }
        ]
    )
    middleware.state_schema = AssistantAsyncTaskState
    middleware.tools = [_authenticated_tool(tool) for tool in middleware.tools]
    return middleware


def _authenticated_tool(
    tool,
):
    description = _ASYNC_TASK_TOOL_DESCRIPTIONS.get(tool.name)
    if description is not None:
        tool = tool.model_copy(update={"description": description})
    if tool.name == "start_async_task":
        return _authenticated_start_tool(tool)
    coroutines = {
        "check_async_task": _check_async_task,
        "update_async_task": _update_async_task,
        "cancel_async_task": _cancel_async_task,
        "list_async_tasks": _list_async_tasks,
    }
    coroutine = coroutines.get(tool.name)
    if coroutine is None:
        return tool
    return tool.model_copy(
        update={
            "func": _async_only,
            "coroutine": coroutine,
        }
    )


def _authenticated_start_tool(
    tool,
):
    def start_async_task(
        description: str,
        subagent_type: str,
        runtime: ToolRuntime,
    ) -> str | Command:
        if subagent_type != BACKGROUND_AGENT_NAME:
            return _unknown_agent(subagent_type)
        headers = _internal_worker_headers(authenticated_user_identity(runtime))
        client = get_sync_client(url=_agent_server_url(), api_key=None)
        try:
            thread_id = str(uuid4())
            parent_thread_id, parent_run_id = _parent_run(runtime)
            metadata = _correlation_metadata(
                thread_id,
                parent_thread_id,
                parent_run_id,
            )
            thread = client.threads.create(
                thread_id=thread_id,
                graph_id=WORKER_GRAPH_ID,
                metadata=metadata,
                headers=headers,
            )
            run = client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=WORKER_GRAPH_ID,
                input={
                    "messages": [{"role": "user", "content": description}],
                    "memory_context": list(runtime.state.get("memory_context") or ()),
                },
                metadata=metadata,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - SDK exceptions are untyped.
            return f"Failed to launch async subagent '{subagent_type}': {exc}"
        finally:
            client.close()
        return _started_task_command(
            thread["thread_id"],
            run["run_id"],
            runtime,
            parent_thread_id=parent_thread_id,
            parent_run_id=parent_run_id,
        )

    async def astart_async_task(
        description: str,
        subagent_type: str,
        runtime: ToolRuntime,
    ) -> str | Command:
        if subagent_type != BACKGROUND_AGENT_NAME:
            return _unknown_agent(subagent_type)
        headers = _internal_worker_headers(authenticated_user_identity(runtime))
        client = get_client(url=_agent_server_url(), api_key=None)
        try:
            thread_id = str(uuid4())
            parent_thread_id, parent_run_id = _parent_run(runtime)
            metadata = _correlation_metadata(
                thread_id,
                parent_thread_id,
                parent_run_id,
            )
            thread = await client.threads.create(
                thread_id=thread_id,
                graph_id=WORKER_GRAPH_ID,
                metadata=metadata,
                headers=headers,
            )
            run = await client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=WORKER_GRAPH_ID,
                input={
                    "messages": [{"role": "user", "content": description}],
                    "memory_context": list(runtime.state.get("memory_context") or ()),
                },
                metadata=metadata,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - SDK exceptions are untyped.
            return f"Failed to launch async subagent '{subagent_type}': {exc}"
        finally:
            await client.aclose()
        return _started_task_command(
            thread["thread_id"],
            run["run_id"],
            runtime,
            parent_thread_id=parent_thread_id,
            parent_run_id=parent_run_id,
        )

    return tool.model_copy(
        update={
            "func": start_async_task,
            "coroutine": astart_async_task,
        }
    )


def _async_only(runtime: ToolRuntime, **_kwargs) -> str:
    del runtime
    return "Async subagent management requires an async Agent Server invocation."


async def _check_async_task(
    task_id: str,
    runtime: ToolRuntime,
) -> str | Command:
    task = _tracked_task(task_id, runtime)
    if isinstance(task, str):
        return task
    headers = _identity_headers(runtime)
    client = get_client(url=_agent_server_url(), api_key=None)
    try:
        run = await client.runs.get(
            thread_id=task["thread_id"],
            run_id=task["run_id"],
            headers=headers,
        )
        result = {"status": run["status"], "thread_id": task["thread_id"]}
        if run["status"] == "success":
            thread = await client.threads.get(
                thread_id=task["thread_id"],
                headers=headers,
            )
            messages = (thread.get("values") or {}).get("messages") or []
            result["result"] = (
                messages[-1].get("content", "")
                if messages and isinstance(messages[-1], dict)
                else "(completed with no output messages)"
            )
        elif run["status"] == "error":
            result["error"] = str(
                run.get("error") or "The async subagent encountered an error."
            )
    except Exception as exc:  # noqa: BLE001 - SDK exceptions are untyped.
        return f"Failed to get run status: {exc}"
    finally:
        await client.aclose()
    return _task_command(
        task,
        runtime,
        content=json.dumps(result),
        status=str(run["status"]),
        checked=True,
    )


async def _update_async_task(
    task_id: str,
    message: str,
    runtime: ToolRuntime,
) -> str | Command:
    task = _tracked_task(task_id, runtime)
    if isinstance(task, str):
        return task
    headers = _internal_worker_headers(authenticated_user_identity(runtime))
    client = get_client(url=_agent_server_url(), api_key=None)
    try:
        metadata = _correlation_metadata(
            task["task_id"],
            task["parent_thread_id"],
            task["parent_run_id"],
        )
        run = await client.runs.create(
            thread_id=task["thread_id"],
            assistant_id=WORKER_GRAPH_ID,
            input={"messages": [{"role": "user", "content": message}]},
            metadata=metadata,
            multitask_strategy="interrupt",
            headers=headers,
        )
    except Exception as exc:  # noqa: BLE001 - SDK exceptions are untyped.
        return f"Failed to update async subagent: {exc}"
    finally:
        await client.aclose()
    updated = {**task, "run_id": run["run_id"]}
    return _task_command(
        updated,
        runtime,
        content=f"Updated async subagent. task_id: {task['task_id']}",
        status="running",
        force_updated=True,
    )


async def _cancel_async_task(
    task_id: str,
    runtime: ToolRuntime,
) -> str | Command:
    task = _tracked_task(task_id, runtime)
    if isinstance(task, str):
        return task
    headers = _identity_headers(runtime)
    client = get_client(url=_agent_server_url(), api_key=None)
    try:
        await client.runs.cancel(
            thread_id=task["thread_id"],
            run_id=task["run_id"],
            headers=headers,
        )
    except Exception as exc:  # noqa: BLE001 - SDK exceptions are untyped.
        return f"Failed to cancel run: {exc}"
    finally:
        await client.aclose()
    return _task_command(
        task,
        runtime,
        content=f"Cancelled async subagent task: {task['task_id']}",
        status="cancelled",
        checked=True,
    )


async def _list_async_tasks(
    runtime: ToolRuntime,
    status_filter: str | None = None,
) -> str | Command:
    tasks = list((runtime.state.get("async_tasks") or {}).values())
    if status_filter not in {None, "all"}:
        tasks = [task for task in tasks if task["status"] == status_filter]
    if not tasks:
        return "No async subagent tasks tracked."
    headers = _identity_headers(runtime)
    client = get_client(url=_agent_server_url(), api_key=None)
    updated = {}
    entries = []
    try:
        # ponytail: sequential polling; use gather if per-thread task counts make this slow.
        for task in tasks:
            status = task["status"]
            if status not in {
                "cancelled",
                "success",
                "error",
                "timeout",
                "interrupted",
            }:
                try:
                    run = await client.runs.get(
                        thread_id=task["thread_id"],
                        run_id=task["run_id"],
                        headers=headers,
                    )
                    status = run["status"]
                except Exception:  # noqa: BLE001 - retain the cached upstream status.
                    pass
            updated[task["task_id"]] = _updated_task(task, status, checked=True)
            entries.append(
                f"- task_id: {task['task_id']}  agent: {task['agent_name']}  status: {status}"
            )
    finally:
        await client.aclose()
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"{len(entries)} tracked task(s):\n" + "\n".join(entries),
                    tool_call_id=runtime.tool_call_id,
                )
            ],
            "async_tasks": updated,
        }
    )


def _started_task_command(
    thread_id: str,
    run_id: str,
    runtime: ToolRuntime,
    *,
    parent_thread_id: str,
    parent_run_id: str,
) -> Command:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    task = {
        "task_id": thread_id,
        "agent_name": BACKGROUND_AGENT_NAME,
        "thread_id": thread_id,
        "run_id": run_id,
        "parent_thread_id": parent_thread_id,
        "parent_run_id": parent_run_id,
        "status": "running",
        "created_at": now,
        "last_checked_at": now,
        "last_updated_at": now,
    }
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Launched async subagent. task_id: {thread_id}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
            "async_tasks": {thread_id: task},
        }
    )


def _tracked_task(task_id: str, runtime: ToolRuntime) -> dict | str:
    task = (runtime.state.get("async_tasks") or {}).get(task_id.strip())
    if not isinstance(task, dict) or task.get("agent_name") != BACKGROUND_AGENT_NAME:
        return f"No tracked task found for task_id: {task_id!r}"
    return task


def _task_command(
    task: dict,
    runtime: ToolRuntime,
    *,
    content: str,
    status: str,
    checked: bool = False,
    force_updated: bool = False,
) -> Command:
    updated = _updated_task(
        task,
        status,
        checked=checked,
        force_updated=force_updated,
    )
    return Command(
        update={
            "messages": [ToolMessage(content, tool_call_id=runtime.tool_call_id)],
            "async_tasks": {task["task_id"]: updated},
        }
    )


def _updated_task(
    task: dict,
    status: str,
    *,
    checked: bool,
    force_updated: bool = False,
) -> dict:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        **task,
        "status": status,
        "last_checked_at": now if checked else task["last_checked_at"],
        "last_updated_at": (
            now
            if force_updated or status != task["status"]
            else task["last_updated_at"]
        ),
    }


def _identity_headers(runtime: ToolRuntime) -> dict[str, str]:
    return {"X-Assistant-User": authenticated_user_identity(runtime)}


def _parent_run(runtime: ToolRuntime) -> tuple[str, str]:
    parent_thread_id = str(
        runtime.config.get("configurable", {}).get("thread_id", "")
    ).strip()
    parent_run_id = str(runtime.config.get("run_id", "")).strip()
    if not parent_thread_id or not parent_run_id:
        raise ValueError("async subagent launch requires parent thread_id and run_id")
    return parent_thread_id, parent_run_id


def _correlation_metadata(
    task_id: str,
    parent_thread_id: str,
    parent_run_id: str,
) -> dict[str, object]:
    return {
        "assistant_agent_task_id": task_id,
        "assistant_agent_parent_thread_id": parent_thread_id,
        "assistant_agent_parent_run_id": parent_run_id,
        **assistant_runtime_metadata(
            AssistantRuntimeFacts(
                entry_profile="async_worker",
            )
        ),
    }


def _agent_server_url() -> str:
    raw_port = os.environ.get("ASSISTANT_AGENT_SERVER_PORT", "8089")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("ASSISTANT_AGENT_SERVER_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("ASSISTANT_AGENT_SERVER_PORT must be between 1 and 65535")
    return f"http://127.0.0.1:{port}"


def _unknown_agent(subagent_type: str) -> str:
    return (
        f"Unknown async subagent type `{subagent_type}`. "
        f"Available types: `{BACKGROUND_AGENT_NAME}`"
    )


__all__ = [
    "ASYNC_TASK_TOOL_NAMES",
    "BACKGROUND_AGENT_NAME",
    "async_task_tool_profile",
    "build_async_subagent_middleware",
]

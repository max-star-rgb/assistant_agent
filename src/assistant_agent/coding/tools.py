"""Standard LangChain tools for governed coding workspaces."""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.coding.models import CodingAnalysisSnapshot, CodingToolScope
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import configure_builtin_tool, invoke_native_tool


def create_coding_tools(service: CodingWorkspaceService) -> list[BaseTool]:
    @tool("coding_repo_list", response_format="content_and_artifact")
    def coding_repo_list(
        runtime: ToolRuntime[AssistantRunContext],
        path: str = "",
        depth: Annotated[int, Field(ge=1, le=8)] = 2,
        cursor: Annotated[int, Field(ge=0)] = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """List a bounded page of files in the current isolated coding workspace."""

        return _invoke(
            "coding_repo_list",
            runtime,
            service,
            lambda workspace: service.list_files(
                workspace,
                path=path,
                depth=depth,
                cursor=cursor,
                limit=100,
            ),
        )

    @tool("coding_repo_search", response_format="content_and_artifact")
    def coding_repo_search(
        query: Annotated[str, Field(min_length=1, max_length=1_000)],
        runtime: ToolRuntime[AssistantRunContext],
        paths: tuple[str, ...] = ("",),
        globs: tuple[str, ...] = (),
        cursor: Annotated[int, Field(ge=0)] = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Search literal text with bounded results in the coding workspace."""

        return _invoke(
            "coding_repo_search",
            runtime,
            service,
            lambda workspace: service.search(
                workspace,
                query=query,
                paths=paths,
                globs=globs,
                cursor=cursor,
                limit=100,
            ),
        )

    @tool("coding_repo_read", response_format="content_and_artifact")
    def coding_repo_read(
        path: Annotated[str, Field(min_length=1, max_length=1_024)],
        start_line: Annotated[int, Field(ge=1)],
        end_line: Annotated[int, Field(ge=1)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read a bounded UTF-8 line range from the coding workspace."""

        return _invoke(
            "coding_repo_read",
            runtime,
            service,
            lambda workspace: service.read(
                workspace,
                path,
                start_line=start_line,
                end_line=end_line,
            ),
        )

    @tool("coding_repo_status", response_format="content_and_artifact")
    def coding_repo_status(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return bounded Git status for the isolated coding workspace."""

        return _invoke(
            "coding_repo_status",
            runtime,
            service,
            service.status,
        )

    @tool("coding_repo_diff", response_format="content_and_artifact")
    def coding_repo_diff(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return the current bounded diff from the isolated coding workspace."""

        return _invoke(
            "coding_repo_diff",
            runtime,
            service,
            service.diff,
        )

    @tool("coding_propose_patch", response_format="content_and_artifact")
    def coding_propose_patch(
        patch: Annotated[str, Field(min_length=1)],
        summary: Annotated[str, Field(min_length=1, max_length=4_000)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Validate one complete candidate patch without applying it."""

        scope = coding_scope(runtime)
        workspace = service.resolve(scope.identity, scope.thread_id, scope.repo_id)

        def operation() -> ToolResult:
            validation = service.validate_patch(workspace, patch, summary)
            data = validation.model_dump(mode="json")
            proposal = validation.proposal
            return ToolResult(
                tool_name="coding_propose_patch",
                success=True,
                data=data,
                model_observation={
                    "status": "valid",
                    "summary": proposal.summary,
                    "changed_paths": list(proposal.changed_paths),
                    "base_commit": proposal.base_commit,
                    "patch_digest": proposal.patch_digest,
                    "diff_preview": validation.diff_preview,
                },
                trace_summary={
                    "status": "valid",
                    "changed_file_count": len(proposal.changed_paths),
                    "patch_digest": proposal.patch_digest,
                },
                audit_payload={
                    "patch_digest": proposal.patch_digest,
                    "changed_paths": list(proposal.changed_paths),
                    "patch_redacted": True,
                },
            )

        return invoke_native_tool("coding_propose_patch", operation)

    tools = [
        configure_builtin_tool(coding_repo_list, "read"),
        configure_builtin_tool(coding_repo_search, "read"),
        configure_builtin_tool(coding_repo_read, "read"),
        configure_builtin_tool(coding_repo_status, "read"),
        configure_builtin_tool(coding_repo_diff, "read"),
        configure_builtin_tool(coding_propose_patch, "generate"),
    ]
    return sorted(tools, key=lambda item: item.name)


def build_coding_analysis_tools(service: CodingWorkspaceService) -> list[BaseTool]:
    @tool("coding_repo_list", response_format="content_and_artifact")
    def coding_repo_list(
        runtime: ToolRuntime[AssistantRunContext],
        path: str = "",
        depth: Annotated[int, Field(ge=1, le=8)] = 2,
        cursor: Annotated[int, Field(ge=0)] = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """List a bounded page of files in the immutable analysis snapshot."""

        return _invoke_analysis(
            "coding_repo_list",
            runtime,
            service,
            lambda snapshot, workspace, scope, live_workspace: service.list_files(
                workspace,
                path=path,
                depth=depth,
                cursor=cursor,
                limit=100,
            ),
        )

    @tool("coding_repo_search", response_format="content_and_artifact")
    def coding_repo_search(
        query: Annotated[str, Field(min_length=1, max_length=1_000)],
        runtime: ToolRuntime[AssistantRunContext],
        paths: tuple[str, ...] = ("",),
        globs: tuple[str, ...] = (),
        cursor: Annotated[int, Field(ge=0)] = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Search literal text with bounded results in the analysis snapshot."""

        return _invoke_analysis(
            "coding_repo_search",
            runtime,
            service,
            lambda snapshot, workspace, scope, live_workspace: service.search(
                workspace,
                query=query,
                paths=paths,
                globs=globs,
                cursor=cursor,
                limit=100,
            ),
        )

    @tool("coding_repo_read", response_format="content_and_artifact")
    def coding_repo_read(
        path: Annotated[str, Field(min_length=1, max_length=1_024)],
        start_line: Annotated[int, Field(ge=1)],
        end_line: Annotated[int, Field(ge=1)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read a bounded UTF-8 line range from the analysis snapshot."""

        return _invoke_analysis(
            "coding_repo_read",
            runtime,
            service,
            lambda snapshot, workspace, scope, live_workspace: service.read_analysis_snapshot(
                snapshot,
                path,
                start_line,
                end_line,
                identity=scope.identity,
                thread_id=scope.thread_id,
                workspace=live_workspace,
            ),
        )

    @tool("coding_repo_status", response_format="content_and_artifact")
    def coding_repo_status(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return bounded Git status captured by the analysis snapshot."""

        return _invoke_analysis(
            "coding_repo_status",
            runtime,
            service,
            lambda snapshot, workspace, scope, live_workspace: service.status_analysis_snapshot(
                snapshot,
                identity=scope.identity,
                thread_id=scope.thread_id,
                workspace=live_workspace,
            ),
        )

    @tool("coding_repo_diff", response_format="content_and_artifact")
    def coding_repo_diff(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return the bounded cumulative diff captured by the analysis snapshot."""

        return _invoke_analysis(
            "coding_repo_diff",
            runtime,
            service,
            lambda snapshot, workspace, scope, live_workspace: service.diff_analysis_snapshot(
                snapshot,
                identity=scope.identity,
                thread_id=scope.thread_id,
                workspace=live_workspace,
            ),
        )

    return sorted(
        (
            configure_builtin_tool(coding_repo_list, "read"),
            configure_builtin_tool(coding_repo_search, "read"),
            configure_builtin_tool(coding_repo_read, "read"),
            configure_builtin_tool(coding_repo_status, "read"),
            configure_builtin_tool(coding_repo_diff, "read"),
        ),
        key=lambda item: item.name,
    )


def coding_scope(runtime: ToolRuntime[AssistantRunContext]) -> CodingToolScope:
    identity = authenticated_user_identity(runtime)
    thread_id = str(runtime.config.get("configurable", {}).get("thread_id", "")).strip()
    repo_id = str(runtime.state.get("coding_repo_id", "")).strip()
    if not thread_id or not repo_id:
        raise ToolException("coding_scope_unavailable")
    return CodingToolScope(identity=identity, thread_id=thread_id, repo_id=repo_id)


def _invoke(
    tool_name: str,
    runtime: ToolRuntime[AssistantRunContext],
    service: CodingWorkspaceService,
    operation,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scope = coding_scope(runtime)
    workspace = service.resolve(scope.identity, scope.thread_id, scope.repo_id)

    def execute() -> ToolResult:
        result = operation(workspace)
        data = result.model_dump(mode="json")
        return ToolResult(
            tool_name=tool_name,
            success=True,
            data=data,
            model_observation=data,
            trace_summary={"status": "succeeded", "workspace_ref": workspace.workspace_ref},
            audit_payload={"workspace_ref": workspace.workspace_ref, "content_redacted": True},
        )

    return invoke_native_tool(tool_name, execute)


def _invoke_analysis(
    tool_name: str,
    runtime: ToolRuntime[AssistantRunContext],
    service: CodingWorkspaceService,
    operation,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    def execute() -> ToolResult:
        scope = coding_scope(runtime)
        workspace_ref = str(runtime.state.get("workspace_ref", "")).strip()
        base_commit = str(runtime.state.get("base_commit", "")).strip()
        if not workspace_ref or not base_commit:
            raise ToolException("coding_analysis_snapshot_mismatch")
        try:
            workspace = service.get(
                workspace_ref,
                identity=scope.identity,
                thread_id=scope.thread_id,
            )
        except Exception as exc:
            raise ToolException("coding_analysis_snapshot_mismatch") from exc
        if (
            workspace.workspace_ref != workspace_ref
            or workspace.base_commit != base_commit
            or workspace.repo_id != scope.repo_id
        ):
            raise ToolException("coding_analysis_snapshot_mismatch")
        raw_snapshot = runtime.state.get("analysis_snapshot")
        try:
            if isinstance(raw_snapshot, CodingAnalysisSnapshot):
                snapshot = raw_snapshot
            else:
                snapshot = CodingAnalysisSnapshot.model_validate_json(
                    json.dumps(raw_snapshot)
                )
        except Exception as exc:
            raise ToolException("coding_analysis_snapshot_mismatch") from exc
        snapshot_workspace = service.resolve_analysis_snapshot(
            snapshot,
            identity=scope.identity,
            thread_id=scope.thread_id,
            workspace=workspace,
        )
        result = operation(snapshot, snapshot_workspace, scope, workspace)
        result_data = result.model_dump(mode="json")
        data = {
            "snapshot_ref": snapshot.snapshot_ref,
            "tree_digest": snapshot.tree_digest,
            "result": result_data,
        }
        return ToolResult(
            tool_name=tool_name,
            success=True,
            data=data,
            model_observation=data,
            trace_summary={
                "status": "succeeded",
                "snapshot_ref": snapshot.snapshot_ref,
                "tree_digest": snapshot.tree_digest,
            },
            audit_payload={
                "snapshot_ref": snapshot.snapshot_ref,
                "tree_digest": snapshot.tree_digest,
                "content_redacted": True,
            },
        )

    return invoke_native_tool(tool_name, execute)


__all__ = [
    "build_coding_analysis_tools",
    "coding_scope",
    "create_coding_tools",
]

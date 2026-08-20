from __future__ import annotations

from assistant_agent.coding.workspace import (
    CodingWorkspaceError,
    CodingWorkspaceService,
)


def test_same_scope_reuses_worktree_and_other_scope_is_isolated(
    coding_config,
    source_repo,
) -> None:
    service = CodingWorkspaceService(coding_config, secret=b"test-secret")

    first = service.resolve("user-a", "thread-a", "repo")
    again = service.resolve("user-a", "thread-a", "repo")
    other = service.resolve("user-b", "thread-a", "repo")

    assert first.workspace_ref == again.workspace_ref
    assert first.root == again.root
    assert other.workspace_ref != first.workspace_ref
    assert first.base_commit == service.git_head(source_repo)
    assert first.root.is_dir()


def test_workspace_identity_mismatch_fails(coding_config) -> None:
    service = CodingWorkspaceService(coding_config, secret=b"test-secret")
    workspace = service.resolve("user-a", "thread-a", "repo")

    try:
        service.get(
            workspace.workspace_ref,
            identity="user-b",
            thread_id="thread-a",
        )
    except CodingWorkspaceError as exc:
        assert exc.code == "workspace_identity_mismatch"
    else:
        raise AssertionError("identity mismatch was accepted")


def test_disabled_service_does_not_create_workspace(coding_config) -> None:
    disabled = coding_config.model_copy(update={"enabled": False})
    service = CodingWorkspaceService(disabled, secret=b"test-secret")

    try:
        service.resolve("user-a", "thread-a", "repo")
    except CodingWorkspaceError as exc:
        assert exc.code == "workspace_not_allowed"
    else:
        raise AssertionError("disabled coding service created a workspace")

    assert not disabled.workspace_root.exists()


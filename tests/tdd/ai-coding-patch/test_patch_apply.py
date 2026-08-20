from __future__ import annotations

import pytest

from assistant_agent.coding.workspace import CodingWorkspaceError


TWO_FILE_PATCH = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-value = 'a'
+value = 'new-a'
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-value = 'b'
+value = 'new-b'
"""


def test_apply_rejects_file_digest_drift_without_partial_write(workspace_bundle) -> None:
    service, workspace = workspace_bundle
    validation = service.validate_patch(workspace, TWO_FILE_PATCH, "change both")
    (workspace.root / "a.py").write_text("external = True\n", encoding="utf-8")
    before_b = (workspace.root / "b.py").read_bytes()

    with pytest.raises(CodingWorkspaceError) as raised:
        service.apply_validated_patch(workspace, validation)

    assert raised.value.code == "file_digest_changed"
    assert (workspace.root / "b.py").read_bytes() == before_b


def test_apply_updates_all_files_and_returns_digest(workspace_bundle) -> None:
    service, workspace = workspace_bundle
    validation = service.validate_patch(workspace, TWO_FILE_PATCH, "change both")

    result = service.apply_validated_patch(workspace, validation)

    assert (workspace.root / "a.py").read_text(encoding="utf-8") == "value = 'new-a'\n"
    assert (workspace.root / "b.py").read_text(encoding="utf-8") == "value = 'new-b'\n"
    assert result.patch_digest == validation.proposal.patch_digest
    assert result.changed_paths == ("a.py", "b.py")


def test_unexpected_apply_failure_restores_all_targets(workspace_bundle, monkeypatch) -> None:
    service, workspace = workspace_bundle
    validation = service.validate_patch(workspace, TWO_FILE_PATCH, "change both")
    original_run_git = service._run_git

    def fail_apply(repo, *args, **kwargs):
        if args[:2] == ("apply", "--whitespace=nowarn"):
            (workspace.root / "a.py").write_text("partial = True\n", encoding="utf-8")
            raise CodingWorkspaceError("patch_apply_failed")
        return original_run_git(repo, *args, **kwargs)

    monkeypatch.setattr(service, "_run_git", fail_apply)

    with pytest.raises(CodingWorkspaceError) as raised:
        service.apply_validated_patch(workspace, validation)

    assert raised.value.code == "patch_apply_failed"
    assert (workspace.root / "a.py").read_text(encoding="utf-8") == "value = 'a'\n"
    assert (workspace.root / "b.py").read_text(encoding="utf-8") == "value = 'b'\n"

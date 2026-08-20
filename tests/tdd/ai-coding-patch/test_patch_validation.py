from __future__ import annotations

import hashlib

import pytest

from assistant_agent.coding.workspace import CodingWorkspaceError


UPDATE_PATCH = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-value = 'a'
+value = 'changed'
"""

NEW_FILE_PATCH = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+created = True
"""


def test_validation_derives_paths_digests_and_preview(workspace_bundle) -> None:
    service, workspace = workspace_bundle

    result = service.validate_patch(workspace, UPDATE_PATCH, "change value")

    assert result.proposal.changed_paths == ("a.py",)
    assert result.proposal.patch_digest == hashlib.sha256(
        UPDATE_PATCH.encode("utf-8")
    ).hexdigest()
    assert result.proposal.base_file_digests["a.py"] == hashlib.sha256(
        b"value = 'a'\n"
    ).hexdigest()
    assert result.diff_preview == UPDATE_PATCH


def test_validation_accepts_new_utf8_text_file(workspace_bundle) -> None:
    service, workspace = workspace_bundle

    result = service.validate_patch(workspace, NEW_FILE_PATCH, "create file")

    assert result.proposal.changed_paths == ("new.py",)
    assert result.proposal.base_file_digests == {"new.py": "absent"}


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        (
            "diff --git a/a.py b/a.py\ndeleted file mode 100644\n--- a/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-value = 'a'\n",
            "patch_operation_forbidden",
        ),
        (
            "diff --git a/a.py b/c.py\nrename from a.py\nrename to c.py\n",
            "patch_operation_forbidden",
        ),
        (
            "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n",
            "path_protected",
        ),
        (
            "diff --git a/image.png b/image.png\nGIT binary patch\nliteral 0\nHcmV?d00001\n",
            "patch_operation_forbidden",
        ),
    ],
)
def test_validation_rejects_forbidden_patch_forms(
    workspace_bundle,
    patch: str,
    code: str,
) -> None:
    service, workspace = workspace_bundle

    with pytest.raises(CodingWorkspaceError) as raised:
        service.validate_patch(workspace, patch, "unsafe")

    assert raised.value.code == code


from __future__ import annotations

from pathlib import Path

from assistant_agent.coding.workspace import (
    CodingWorkspaceError,
    CodingWorkspaceService,
)


def _workspace(coding_config):
    service = CodingWorkspaceService(coding_config, secret=b"test-secret")
    workspace = service.resolve("user-a", "thread-a", "repo")
    return service, workspace


def test_list_search_and_read_are_bounded(coding_config) -> None:
    service, workspace = _workspace(coding_config)

    listing = service.list_files(workspace, path="", depth=2, cursor=0, limit=2)
    search = service.search(
        workspace,
        query="hello",
        paths=("",),
        globs=("*.py", "*.md"),
        cursor=0,
        limit=10,
    )
    read = service.read(workspace, "src/app.py", start_line=1, end_line=20)

    assert [entry.path for entry in listing.entries] == ["README.md", "src"]
    assert listing.next_cursor == 2
    assert {(match.path, match.line_number) for match in search.matches} == {
        ("README.md", 2),
        ("src/app.py", 1),
    }
    assert read.content == "greeting = 'hello'\n"
    assert read.total_lines == 1


def test_status_and_diff_report_workspace_changes(coding_config) -> None:
    service, workspace = _workspace(coding_config)
    (workspace.root / "src" / "app.py").write_text("greeting = 'changed'\n", encoding="utf-8")

    status = service.status(workspace)
    diff = service.diff(workspace)

    assert status.entries == (" M src/app.py",)
    assert "-greeting = 'hello'" in diff.diff
    assert "+greeting = 'changed'" in diff.diff


def test_read_rejects_symlink_escape(coding_config, tmp_path: Path) -> None:
    service, workspace = _workspace(coding_config)
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    (workspace.root / "linked.py").symlink_to(outside)

    try:
        service.read(workspace, "linked.py", start_line=1, end_line=10)
    except CodingWorkspaceError as exc:
        assert exc.code == "symlink_escape"
    else:
        raise AssertionError("symlink escape was readable")


def test_read_rejects_non_utf8(coding_config) -> None:
    service, workspace = _workspace(coding_config)
    (workspace.root / "src" / "binary.py").write_bytes(b"\xff\xfe")

    try:
        service.read(workspace, "src/binary.py", start_line=1, end_line=10)
    except CodingWorkspaceError as exc:
        assert exc.code == "file_encoding_unsupported"
    else:
        raise AssertionError("non-UTF-8 content was readable")

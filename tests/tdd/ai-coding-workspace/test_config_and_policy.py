from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.policy import CodingPathPolicy, CodingPolicyError


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_coding_config_loads_allowlisted_absolute_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = {
        "MULTIMODAL_AGENT_CODING_ENABLED": "true",
        "MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON": json.dumps(
            {"assistant-agent": {"path": str(repo), "target_branch": "main"}}
        ),
    }

    config = CodingConfig.from_env(env)

    assert config.enabled is True
    assert config.repositories["assistant-agent"].path == repo.resolve()
    assert config.repositories["assistant-agent"].target_branch == "main"


def test_enabled_coding_config_requires_repository_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repository allowlist"):
        CodingConfig.from_env(
            {
                "MULTIMODAL_AGENT_CODING_ENABLED": "true",
                "MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT": str(tmp_path),
                "MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON": "{}",
            }
        )


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/etc/passwd", "path_invalid"),
        ("../secret", "path_invalid"),
        (".git/config", "path_protected"),
        (".env", "path_protected"),
        ("certs/service.pem", "path_protected"),
    ],
)
def test_write_policy_rejects_unsafe_paths(
    tmp_path: Path,
    path: str,
    code: str,
) -> None:
    with pytest.raises(CodingPolicyError) as raised:
        CodingPathPolicy().validate_relative_path(
            tmp_path,
            path,
            operation="write",
        )

    assert raised.value.code == code


def test_write_policy_rejects_symlink_components(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CodingPolicyError) as raised:
        CodingPathPolicy().validate_relative_path(
            root,
            "linked/app.py",
            operation="write",
        )

    assert raised.value.code == "symlink_escape"


def test_write_policy_accepts_utf8_source_path(tmp_path: Path) -> None:
    path = CodingPathPolicy().validate_relative_path(
        tmp_path,
        "src/app.py",
        operation="write",
    )

    assert path == tmp_path / "src/app.py"

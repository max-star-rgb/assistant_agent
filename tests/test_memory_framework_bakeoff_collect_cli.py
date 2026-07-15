import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/lenovo1/miniconda3/envs/hello_agent/bin/python"
SCRIPT = "scripts/collect_memory_framework_bakeoff.py"


def _run(tmp_path: Path, *, env: dict[str, str], extra: list[str] | None = None):
    evidence_dir = tmp_path / "evidence"
    completed = subprocess.run(
        [
            PYTHON,
            SCRIPT,
            "--phase",
            "smoke",
            "--framework",
            "hindsight",
            "--evidence-dir",
            str(evidence_dir),
            *(extra or []),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, evidence_dir


def test_collect_cli_rejects_wrong_runtime_profile_before_creating_evidence(tmp_path) -> None:
    completed, evidence_dir = _run(
        tmp_path,
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "mock",
            "MEMORY_BAKEOFF_API_KEY": "not-written",
        },
    )

    assert completed.returncode == 2
    assert "memory_bakeoff_profile_not_allowed" in completed.stderr
    assert not evidence_dir.exists()


def test_collect_cli_rejects_missing_single_dashscope_credential(tmp_path) -> None:
    env = {
        "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
        "MEMORY_BAKEOFF_API_KEY": "",
    }
    completed, evidence_dir = _run(tmp_path, env=env)

    assert completed.returncode == 2
    assert "memory_bakeoff_missing_api_key" in completed.stderr
    assert not evidence_dir.exists()


def test_collect_cli_rejects_unpinned_version_without_docker_or_evidence(tmp_path) -> None:
    completed, evidence_dir = _run(
        tmp_path,
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "pilot",
            "MEMORY_BAKEOFF_API_KEY": "not-written",
        },
        extra=["--version", "latest"],
    )

    assert completed.returncode == 2
    assert "memory_bakeoff_version_not_pinned" in completed.stderr
    assert not evidence_dir.exists()


def test_collect_cli_sanitizes_docker_preflight_failure_and_keeps_evidence_absent(tmp_path) -> None:
    marker = "credential-must-not-appear"
    completed, evidence_dir = _run(
        tmp_path,
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "pilot",
            "MEMORY_BAKEOFF_API_KEY": marker,
            "PATH": "/nonexistent",
        },
    )

    assert completed.returncode == 2
    assert "memory_bakeoff_docker_command_failed" in completed.stderr
    assert marker not in completed.stderr
    assert not evidence_dir.exists()


def test_collect_cli_help_documents_fixed_provider_and_no_provider_payload_output() -> None:
    completed = subprocess.run(
        [PYTHON, SCRIPT, "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    normalized_help = completed.stdout.replace("\n", "").replace("  ", " ")
    assert "qwen-plus" in normalized_help
    assert "text-embedding-v4" in normalized_help
    assert "raw provider" in normalized_help.lower()


def test_collect_cli_resets_dedicated_volumes_and_maps_one_key_to_both_providers() -> None:
    source = (REPO_ROOT / SCRIPT).read_text(encoding="utf-8")

    assert 'self._compose("down", "--volumes", "--remove-orphans")' in source
    assert '"MEMORY_BAKEOFF_CHAT_API_KEY": api_key' in source
    assert '"MEMORY_BAKEOFF_EMBEDDING_API_KEY": api_key' in source


def test_collect_cli_sanitizes_unexpected_collection_failure(monkeypatch, capsys) -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    marker = "raw-provider-payload-must-not-appear"
    monkeypatch.setenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke")
    monkeypatch.setenv("MEMORY_BAKEOFF_API_KEY", "not-written")
    monkeypatch.setattr(module.DockerComposeLifecycle, "preflight", lambda self: None)
    monkeypatch.setattr(
        module.DockerComposeLifecycle,
        "reset_and_start",
        lambda self: (_ for _ in ()).throw(RuntimeError(marker)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [SCRIPT, "--phase", "smoke", "--framework", "hindsight"],
    )

    assert module.main() == 2
    captured = capsys.readouterr()
    assert "memory_bakeoff_collection_failed" in captured.err
    assert marker not in captured.err

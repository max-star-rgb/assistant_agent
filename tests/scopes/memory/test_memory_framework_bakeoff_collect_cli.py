import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
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

    assert 'self._compose("down", "--volumes", "--remove-orphans", profile=self.framework)' in source
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


def test_empty_volume_startup_budgets_cover_slow_local_docker_storage() -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff_timeouts", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._startup_timeout_seconds("hindsight") == 900.0
    assert module._startup_timeout_seconds("mem0") == 600.0

    compose = (REPO_ROOT / "docker/memory-frameworks/compose.yaml").read_text(encoding="utf-8")
    assert 'HINDSIGHT_API_STARTUP_WAIT_SECONDS: "900"' in compose


def test_health_wait_fails_immediately_when_sidecar_exits(monkeypatch) -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff_exit", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    lifecycle = module.DockerComposeLifecycle.__new__(module.DockerComposeLifecycle)
    lifecycle.framework = "hindsight"
    lifecycle.service = "hindsight"
    lifecycle.base_url = "http://127.0.0.1:8889"
    monkeypatch.setattr(lifecycle, "_container_id", lambda service: "container-id")
    monkeypatch.setattr(lifecycle, "_container_running", lambda container_id: False)

    try:
        lifecycle._wait_healthy(timeout_seconds=900)
    except module.BakeoffCliError as exc:
        assert exc.error_code == "memory_bakeoff_sidecar_exited"
    else:
        raise AssertionError("exited sidecar must abort health wait")


def test_hindsight_lifecycle_builds_the_derived_image_before_start(monkeypatch) -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff_build", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    lifecycle = module.DockerComposeLifecycle(framework="hindsight", api_key="not-written")
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "_compose",
        lambda *args, profile=None: calls.append((args, profile)),
    )
    monkeypatch.setattr(lifecycle, "_wait_healthy", lambda: None)

    lifecycle.reset_and_start()

    assert calls[0] == (("down", "--volumes", "--remove-orphans"), "hindsight")
    assert calls[1] == (("up", "-d", "--build", "hindsight"), "hindsight")


def test_mem0_lifecycle_uses_pinned_image_without_build_during_start(monkeypatch) -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff_mem0_start", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    lifecycle = module.DockerComposeLifecycle(framework="mem0", api_key="not-written")
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "_compose",
        lambda *args, profile=None: calls.append((args, profile)),
    )
    monkeypatch.setattr(lifecycle, "_wait_healthy", lambda: None)

    lifecycle.reset_and_start()

    assert calls[0] == (("down", "--volumes", "--remove-orphans"), "mem0")
    assert calls[1] == (("up", "-d", "--no-build", "mem0"), "mem0")


def test_mem0_health_wait_uses_readiness_endpoint_with_long_inflight_timeout(monkeypatch) -> None:
    spec = spec_from_file_location("collect_memory_framework_bakeoff_mem0_ready", REPO_ROOT / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    lifecycle = module.DockerComposeLifecycle.__new__(module.DockerComposeLifecycle)
    lifecycle.framework = "mem0"
    lifecycle.service = "mem0"
    lifecycle.base_url = "http://127.0.0.1:8890"
    observed = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(lifecycle, "_container_id", lambda service: "container-id")
    monkeypatch.setattr(lifecycle, "_container_running", lambda container_id: True)

    def fake_urlopen(url: str, timeout: float):
        observed["url"] = url
        observed["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    lifecycle._wait_healthy(timeout_seconds=600)

    assert observed["url"] == "http://127.0.0.1:8890/ready"
    assert observed["timeout"] >= 30

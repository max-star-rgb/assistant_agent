import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/lenovo1/miniconda3/envs/hello_agent/bin/python"


def _metrics(framework: str) -> dict:
    return {
        "framework": framework,
        "version": "0.8.4" if framework == "hindsight" else "2.0.11",
        "recall_at_5": 0.9,
        "mrr": 0.85,
        "write_precision": 0.9,
        "contradiction_accuracy": 0.8,
        "temporal_accuracy": 0.8,
        "multihop_accuracy": 0.8,
        "chinese_accuracy": 0.9,
        "episodic_procedural_accuracy": 0.8,
        "false_positive_rate": 0.05,
        "cross_user_leakage_rate": 0.0,
        "crud_history_verified": True,
        "export_delete_clear_verified": True,
        "audit_mapping_verified": True,
        "langgraph_context_verified": True,
        "governance_bypass_detected": False,
        "p95_retain_ms": 180,
        "p95_recall_ms": 120,
        "rss_mb": 500,
        "disk_mb": 300,
        "cold_start_seconds": 15,
        "restart_recovery_verified": True,
        "no_silent_write_loss": True,
        "default_tests_offline": True,
        "backup_portable": True,
        "configuration_steps": 5,
    }


def test_cli_scores_measured_files_and_writes_reproducible_report(tmp_path) -> None:
    hindsight = tmp_path / "hindsight.json"
    mem0 = tmp_path / "mem0.json"
    output = tmp_path / "report.json"
    hindsight.write_text(json.dumps(_metrics("hindsight")), encoding="utf-8")
    mem0.write_text(json.dumps(_metrics("mem0")), encoding="utf-8")

    completed = subprocess.run(
        [
            PYTHON,
            "scripts/run_memory_framework_bakeoff.py",
            "--hindsight-metrics",
            str(hindsight),
            "--mem0-metrics",
            str(mem0),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["fixed_versions"] == {"hindsight": "0.8.4", "mem0": "2.0.11"}
    assert payload["decision"]["recommendation"] == "use_framework"
    assert "generated_at" not in payload


def test_compose_uses_pinned_images_local_ports_and_persistent_volumes() -> None:
    compose = (REPO_ROOT / "docker/memory-frameworks/compose.yaml").read_text(encoding="utf-8")

    assert "name: assistant-agent-memory-bakeoff" in compose
    assert "ghcr.io/vectorize-io/hindsight:0.8.4" in compose
    assert "assistant-agent/mem0-sidecar:2.0.11" in compose
    assert "qdrant/qdrant:v1.15.4" in compose
    assert ":latest" not in compose
    assert '"127.0.0.1:8889:8888"' in compose
    assert '"127.0.0.1:8890:8000"' in compose
    assert "hindsight_data:" in compose
    assert "mem0_history:" in compose
    assert "qdrant_data:" in compose
    assert "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY:" in compose
    assert "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL:" in compose
    assert "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL:" in compose
    assert "HINDSIGHT_API_EMBEDDINGS_API_KEY:" not in compose
    dockerfile = (REPO_ROOT / "docker/memory-frameworks/Mem0.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "mem0ai==2.0.11" in dockerfile
    assert ":latest" not in dockerfile
    sidecar = (REPO_ROOT / "docker/memory-frameworks/mem0_sidecar.py").read_text(
        encoding="utf-8"
    )
    assert '"embedding_dims": 1024' in sidecar
    assert '"embedding_model_dims": 1024' in sidecar
    assert "limit=int(payload.get(\"top_k\")" in sidecar

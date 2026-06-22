import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


DEMO_SCRIPT = Path("scripts/demo_assistant_loop.py")


def test_demo_assistant_loop_loads_env_file_without_overwriting(tmp_path, monkeypatch) -> None:
    module = _load_demo_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke",
                "MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=placeholder",
                "DEEPSEEK_CHAT_MODEL=deepseek-chat",
            ]
        ),
        encoding="utf-8",
    )

    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_CHAT_MODEL",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    monkeypatch.delenv("MULTIMODAL_AGENT_CHAT_PROVIDER", raising=False)
    try:
        loaded = module.load_env_file(env_file)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert loaded["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "deepseek"
    assert "DEEPSEEK_API_KEY" in loaded


def test_demo_assistant_loop_env_loader_strips_smart_quotes(tmp_path, monkeypatch) -> None:
    module = _load_demo_module()
    env_file = tmp_path / ".env"
    env_file.write_text("ARK_API_KEY=“placeholder”\n", encoding="utf-8")
    previous = os.environ.get("ARK_API_KEY")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    try:
        loaded = module.load_env_file(env_file)
    finally:
        if previous is None:
            os.environ.pop("ARK_API_KEY", None)
        else:
            os.environ["ARK_API_KEY"] = previous

    assert loaded["ARK_API_KEY"] == "placeholder"


def test_demo_assistant_loop_does_not_load_qwen_image_default_size_from_env(tmp_path, monkeypatch) -> None:
    module = _load_demo_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke",
                "MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen",
                "DASHSCOPE_API_KEY=placeholder",
                "QWEN_IMAGE_DEFAULT_SIZE=256*256",
            ]
        ),
        encoding="utf-8",
    )

    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "DASHSCOPE_API_KEY",
        "QWEN_IMAGE_DEFAULT_SIZE",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    monkeypatch.delenv("QWEN_IMAGE_DEFAULT_SIZE", raising=False)
    try:
        module.load_env_file(env_file)
        config = module.ProviderConfig.from_env()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert config.image_generation_provider == "qwen"
    assert config.qwen_image_default_size == "1024*1024"


def test_demo_assistant_loop_mock_run_prints_timeline_by_default() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "生成一张白色运动鞋的电商主图",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[run] started" in result.stdout
    assert "[plan] image_generation" in result.stdout
    assert "[tool:image_generation] running..." in result.stdout
    assert "[tool:image_generation] succeeded" in result.stdout
    assert "[answer]" in result.stdout
    assert "Summary" in result.stdout
    assert "tools: image_generation" in result.stdout
    assert "event |" not in result.stdout
    assert "trace |" not in result.stdout
    assert "Decision Trace" not in result.stdout
    assert "thought/decision" not in result.stdout
    assert "DEEPSEEK_API_KEY" not in result.stdout
    assert "Authorization" not in result.stdout
    assert "Traceback" not in result.stderr


def test_demo_assistant_loop_debug_events_prints_raw_events() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "--debug-events",
            "生成一张白色运动鞋的电商主图",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "event | task_started" in result.stdout
    assert "trace | agent_trace_decision" in result.stdout
    assert "event | tool_started" in result.stdout


def test_demo_assistant_loop_show_trace_prints_full_decision_trace() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "--no-live-events",
            "--show-trace",
            "生成一张白色运动鞋的电商主图",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Timeline" in result.stdout
    assert "Decision Trace" in result.stdout
    assert "decision_summary:" in result.stdout
    assert "action: image_generation" in result.stdout


def test_demo_assistant_loop_json_mode_does_not_print_live_events() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "--json",
            "你好",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.lstrip().startswith("{")
    assert "event |" not in result.stdout
    assert "Traceback" not in result.stderr


def test_demo_assistant_loop_redacts_signed_image_urls_for_display() -> None:
    module = _load_demo_module()

    redacted = module._safe_display_value(
        "https://example.com/image.png?X-Tos-Algorithm=TOS4-HMAC-SHA256&X-Tos-Credential=secret&X-Tos-Signature=sig"
    )

    assert redacted == "https://example.com/image.png?[signed-url-redacted]"
    assert "secret" not in redacted
    assert "Signature" not in redacted


def test_demo_assistant_loop_does_not_infer_image_provider_from_ark_key(monkeypatch) -> None:
    module = _load_demo_module()
    keys = ["ARK_API_KEY", "MULTIMODAL_AGENT_IMAGE_PROVIDER", "MULTIMODAL_AGENT_RUNTIME_PROFILE"]
    previous = {key: os.environ.get(key) for key in keys}
    try:
        monkeypatch.setenv("ARK_API_KEY", "placeholder")
        monkeypatch.delenv("MULTIMODAL_AGENT_IMAGE_PROVIDER", raising=False)
        monkeypatch.delenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", raising=False)

        args = module.build_parser().parse_args([])
        module._apply_demo_image_provider_default(args)

        assert "MULTIMODAL_AGENT_IMAGE_PROVIDER" not in os.environ
        assert "MULTIMODAL_AGENT_RUNTIME_PROFILE" not in os.environ
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_demo_assistant_loop_explicit_image_provider_sets_runtime_provider(monkeypatch) -> None:
    module = _load_demo_module()
    keys = ["ARK_API_KEY", "MULTIMODAL_AGENT_IMAGE_PROVIDER", "MULTIMODAL_AGENT_RUNTIME_PROFILE"]
    previous = {key: os.environ.get(key) for key in keys}
    try:
        monkeypatch.setenv("ARK_API_KEY", "placeholder")
        monkeypatch.delenv("MULTIMODAL_AGENT_IMAGE_PROVIDER", raising=False)

        args = module.build_parser().parse_args(["--image-provider", "mock"])
        module._apply_demo_image_provider_default(args)

        assert os.environ["MULTIMODAL_AGENT_IMAGE_PROVIDER"] == "mock"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_demo_assistant_loop_saves_replayable_run_log(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "--save-log",
            str(log_dir),
            "生成一张白色运动鞋的电商主图",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    saved_logs = list(log_dir.glob("run_*.json"))
    assert len(saved_logs) == 1
    payload = json.loads(saved_logs[0].read_text(encoding="utf-8"))
    assert payload["demo_metadata"]["request"]["query"] == "生成一张白色运动鞋的电商主图"
    assert payload["events"]
    assert "Replay command" in result.stdout


def test_demo_assistant_loop_replays_saved_log(tmp_path) -> None:
    log_path = tmp_path / "saved.json"
    log_path.write_text(
        json.dumps(
            {
                "query": "旧字段兼容",
                "demo_metadata": {
                    "request": {
                        "query": "你好",
                        "image_refs": [],
                        "video_refs": [],
                        "user_id": "replay_user",
                        "session_id": "replay_session",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "--image-provider",
            "mock",
            "--no-live-events",
            "--replay-log",
            str(log_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Query" in result.stdout
    assert "你好" in result.stdout
    assert "event |" not in result.stdout
    assert "Timeline" in result.stdout
    assert "[answer]" in result.stdout


def test_demo_assistant_loop_deepseek_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "deepseek",
            "你好",
        ],
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "DEEPSEEK_API_KEY" in result.stdout
    assert "Authorization" not in result.stdout
    assert "Traceback" not in result.stderr


def _load_demo_module():
    module_name = "demo_assistant_loop_test"
    spec = importlib.util.spec_from_file_location(module_name, DEMO_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

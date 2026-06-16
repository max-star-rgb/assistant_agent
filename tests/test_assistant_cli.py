import json
import subprocess
import sys

from scripts.run_assistant_cli import run_text_prompt


def test_assistant_cli_text_prompt_returns_core_fields() -> None:
    payload = run_text_prompt("帮我写一段商品介绍")

    assert payload["status"] == "succeeded"
    assert payload["response_text"] != "已完成请求处理。"
    assert "商品介绍" in payload["response_text"]
    assert isinstance(payload["tool_sequence"], list)
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")
    assert payload["errors"] == []
    assert payload["offline"] is True


def test_assistant_cli_json_output_from_text() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_assistant_cli.py", "--text", "生成一张日系极简海报"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "succeeded"
    assert payload["tool_sequence"] == ["image_generation"]
    assert "图片" in payload["response_text"]
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")


def test_assistant_cli_scenario_reuses_demo_matrix() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_assistant_cli.py", "--scenario", "product_search_compare"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["scenario_id"] == "product_search_compare"
    assert payload["tool_sequence"] == ["product_search", "price_compare"]
    assert "价格" in payload["response_text"]
    assert payload["offline"] is True


def test_assistant_cli_text_format_includes_trace_fields() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_assistant_cli.py",
            "--text",
            "看看这张图里有什么商品",
            "--image-ref",
            "demo_image_product_1",
            "--format",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "response_text:" in result.stdout
    assert "tool_sequence: vision_understanding" in result.stdout
    assert "run_id: run_" in result.stdout
    assert "trace_id: trace_" in result.stdout

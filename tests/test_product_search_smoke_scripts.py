import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/smoke_product_search.py")
DEMO_PRODUCTS = Path("demo_data/products/products.example.json")


def test_product_search_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_product_search_import_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("smoke script import must not call provider")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_product_search_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--query", "500 元以内的白色运动鞋"],
        env={"MULTIMODAL_AGENT_PRODUCT_PROVIDER": "mock"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "mock"
    assert payload["capability"] == "product_search"
    assert payload["item_count"] >= 1
    assert payload["items"][0]["product_id"]
    assert payload["items"][0]["ranking_reason"]["explanation"]
    assert payload["errors"] == []


def test_product_search_smoke_local_json_outputs_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--query",
            "白色运动鞋",
            "--local-json",
            str(DEMO_PRODUCTS),
        ],
        env={"MULTIMODAL_AGENT_PRODUCT_PROVIDER": "local_json"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "local_json"
    assert payload["items"][0]["source"] == "local_json"
    assert "provider_response" not in payload


def test_product_search_smoke_http_missing_config_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--query", "白色运动鞋"],
        env={"MULTIMODAL_AGENT_PRODUCT_PROVIDER": "http"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "PRODUCT_SEARCH_BASE_URL" in result.stdout
    assert "PRODUCT_SEARCH_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr

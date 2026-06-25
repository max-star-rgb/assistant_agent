import importlib.util
import os
from pathlib import Path


SCRIPT_PATH = Path("scripts/run_server.py")


def _load_module(name: str = "run_server_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_server_script_import_is_safe() -> None:
    module = _load_module()

    assert hasattr(module, "main")


def test_run_server_parser_defaults() -> None:
    module = _load_module("run_server_parser_test")

    args = module.build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.public_url is None
    assert args.reload is False
    assert args.env_file == ".env"
    assert args.no_env_file is False
    assert args.trial_user_id == []
    assert args.trial_user_id_file is None
    assert args.provider is None
    assert args.image_provider is None


def test_run_server_parser_accepts_public_url() -> None:
    module = _load_module("run_server_parser_public_url_test")

    args = module.build_parser().parse_args(["--host", "0.0.0.0", "--public-url", "http://demo.local/demo/console"])

    assert args.host == "0.0.0.0"
    assert args.public_url == "http://demo.local/demo/console"


def test_run_server_provider_override_enables_provider_smoke(monkeypatch) -> None:
    module = _load_module("run_server_provider_override_test")
    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "MULTIMODAL_AGENT_SKIP_DOTENV",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    args = module.build_parser().parse_args(
        ["--no-env-file", "--provider", "deepseek", "--image-provider", "mock"]
    )
    loaded = module._prepare_environment(args)

    assert loaded == {}
    assert os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "deepseek"
    assert os.environ["MULTIMODAL_AGENT_IMAGE_PROVIDER"] == "mock"
    assert os.environ["MULTIMODAL_AGENT_SKIP_DOTENV"] == "1"


def test_run_server_runtime_summary_prints_product_providers(monkeypatch, capsys) -> None:
    module = _load_module("run_server_runtime_summary_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_TRIAL_USER_IDS", raising=False)

    config = module.ProviderConfig(
        product_search_provider="haodanku",
        price_compare_provider="haodanku",
    )
    module._print_runtime_summary(config, loaded_env_keys=[])
    output = capsys.readouterr().out

    assert "product_search_provider: haodanku" in output
    assert "price_compare_provider: haodanku" in output


def test_run_server_configures_trial_user_allowlist(monkeypatch, tmp_path) -> None:
    module = _load_module("run_server_trial_access_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_TRIAL_USER_IDS", raising=False)
    users_file = tmp_path / "trial-users.txt"
    users_file.write_text("bob\ncarol, phone demo\n", encoding="utf-8")

    args = module.build_parser().parse_args(
        [
            "--no-env-file",
            "--trial-user-id",
            "alice,dave",
            "--trial-user-id-file",
            str(users_file),
        ]
    )
    try:
        module._prepare_environment(args)

        assert os.environ["MULTIMODAL_AGENT_TRIAL_USER_IDS"] == "alice,bob,carol,dave,phone_demo"
    finally:
        os.environ.pop("MULTIMODAL_AGENT_TRIAL_USER_IDS", None)

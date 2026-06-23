import importlib.util
from pathlib import Path


SCRIPT_PATH = Path("scripts/run_web_demo.py")


def test_run_web_demo_script_import_is_safe() -> None:
    spec = importlib.util.spec_from_file_location("run_web_demo_import_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_run_web_demo_parser_defaults() -> None:
    spec = importlib.util.spec_from_file_location("run_web_demo_parser_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.public_url is None
    assert args.reload is False


def test_run_web_demo_parser_accepts_public_url() -> None:
    spec = importlib.util.spec_from_file_location("run_web_demo_parser_public_url_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args(["--host", "0.0.0.0", "--public-url", "http://demo.local/demo/console"])

    assert args.host == "0.0.0.0"
    assert args.public_url == "http://demo.local/demo/console"

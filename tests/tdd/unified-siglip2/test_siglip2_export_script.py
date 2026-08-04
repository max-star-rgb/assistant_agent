import importlib.util
from pathlib import Path
import sys


def _load_script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "export_siglip2_embedding_onnx.py"
    spec = importlib.util.spec_from_file_location("siglip2_joint_export", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_joint_export_manifest_names_both_projections() -> None:
    module = _load_script()
    revision = "a" * 40

    manifest = module.build_joint_manifest(
        model_id="google/siglip2-base-patch16-224",
        model_revision=revision,
        dimension=768,
        image_model_file="vision_model.onnx",
        image_sha256="b" * 64,
        image_external_data={"vision_model.onnx.data": "c" * 64},
        text_model_file="text_model.onnx",
        text_sha256="d" * 64,
        text_external_data={"text_model.onnx.data": "e" * 64},
        tokenizer_file="tokenizer.json",
        tokenizer_sha256="f" * 64,
        max_length=64,
    )

    assert manifest["supported_modalities"] == ["image", "text"]
    assert manifest["image"]["projection"] == "visual_projection"
    assert manifest["text"]["projection"] == "text_projection"
    assert manifest["embedding_space_id"].endswith(":joint-projection-v1")
    assert manifest["image"]["model_revision"] == manifest["text"]["model_revision"]


def test_joint_export_help_is_offline() -> None:
    module = _load_script()
    parser = module.build_parser()

    assert parser.parse_args(["--revision", "a" * 40, "--output-dir", "/tmp/model"]).revision == "a" * 40

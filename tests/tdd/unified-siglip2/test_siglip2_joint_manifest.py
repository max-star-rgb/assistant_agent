import hashlib
import json
from pathlib import Path

import pytest

from assistant_agent.media.embedding.local_siglip2 import (
    LocalSiglip2EmbeddingConfig,
    LocalSiglip2EmbeddingProvider,
    LocalSiglip2Error,
    load_siglip2_embedding_manifest,
)


REVISION = "a" * 40


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_joint_manifest(root: Path, *, text_revision: str = REVISION) -> Path:
    root.mkdir()
    image = b"image-onnx"
    text = b"text-onnx"
    tokenizer = b"tokenizer"
    (root / "vision_model.onnx").write_bytes(image)
    (root / "text_model.onnx").write_bytes(text)
    (root / "tokenizer.json").write_bytes(tokenizer)
    manifest = {
        "schema_version": 2,
        "model_id": "google/siglip2-base-patch16-224",
        "model_revision": REVISION,
        "dimension": 3,
        "embedding_space_id": f"siglip2-base-p16-224@{REVISION}:joint-projection-v1",
        "supported_modalities": ["image", "text"],
        "image": {
            "model_revision": REVISION,
            "model_file": "vision_model.onnx",
            "model_sha256": _sha256(image),
            "external_data": {},
            "projection": "visual_projection",
            "input_name": "pixel_values",
            "output_name": "image_embeds",
            "input_dtype": "float16",
            "preprocessing": {"size": 224, "mean": [0.5] * 3, "std": [0.5] * 3},
        },
        "text": {
            "model_revision": text_revision,
            "model_file": "text_model.onnx",
            "model_sha256": _sha256(text),
            "external_data": {},
            "projection": "text_projection",
            "input_names": ["input_ids", "attention_mask"],
            "output_name": "text_embeds",
            "input_dtype": "int64",
            "tokenizer_file": "tokenizer.json",
            "tokenizer_sha256": _sha256(tokenizer),
            "preprocessing": {"max_length": 64, "padding": "max_length", "truncation": True},
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _write_image_manifest(root: Path) -> Path:
    root.mkdir()
    image = b"image-onnx"
    (root / "vision_model.onnx").write_bytes(image)
    manifest = {
        "schema_version": 1,
        "model_id": "google/siglip2-base-patch16-224",
        "model_revision": REVISION,
        "model_file": "vision_model.onnx",
        "model_sha256": _sha256(image),
        "dimension": 3,
        "embedding_space_id": f"siglip2-base-p16-224@{REVISION}:image-projection-v1",
        "projection": "visual_projection",
        "input_name": "pixel_values",
        "output_name": "image_embeds",
        "input_dtype": "float16",
        "external_data": {},
        "preprocessing": {"size": 224, "mean": [0.5] * 3, "std": [0.5] * 3},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_joint_manifest_requires_one_revision_and_space(tmp_path, monkeypatch) -> None:
    root = _write_joint_manifest(tmp_path / "model", text_revision="b" * 40)
    monkeypatch.setattr(
        "assistant_agent.media.embedding.local_siglip2.onnx_external_data_locations",
        lambda _path: set(),
    )

    with pytest.raises(LocalSiglip2Error, match="manifest_model_revision_mismatch"):
        load_siglip2_embedding_manifest(root)


def test_joint_manifest_exposes_both_modalities(tmp_path, monkeypatch) -> None:
    root = _write_joint_manifest(tmp_path / "model")
    monkeypatch.setattr(
        "assistant_agent.media.embedding.local_siglip2.onnx_external_data_locations",
        lambda _path: set(),
    )

    manifest = load_siglip2_embedding_manifest(root)

    assert manifest.supported_modalities == ("image", "text")
    assert manifest.image is not None
    assert manifest.text is not None
    assert manifest.image.projection == "visual_projection"
    assert manifest.text.projection == "text_projection"


def test_missing_directory_reports_both_modalities_unavailable(tmp_path) -> None:
    provider = LocalSiglip2EmbeddingProvider(
        LocalSiglip2EmbeddingConfig(model_dir=tmp_path / "missing")
    )

    readiness = provider.readiness()

    assert readiness.image_ready is False
    assert readiness.text_ready is False
    assert readiness.issues == ["local_model_unavailable"]


def test_image_only_manifest_reports_text_unavailable(tmp_path, monkeypatch) -> None:
    root = _write_image_manifest(tmp_path / "model")
    monkeypatch.setattr(
        "assistant_agent.media.embedding.local_siglip2.onnx_external_data_locations",
        lambda _path: set(),
    )
    provider = LocalSiglip2EmbeddingProvider(LocalSiglip2EmbeddingConfig(model_dir=root))

    readiness = provider.readiness()

    assert readiness.image_ready is True
    assert readiness.text_ready is False
    assert readiness.issues == ["text_modality_unavailable"]

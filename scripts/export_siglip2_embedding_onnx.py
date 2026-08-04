#!/usr/bin/env python3
"""Operator-only export of matching SigLIP2 image and text ONNX projections."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence


APPROVED_MODEL_ID = "google/siglip2-base-patch16-224"
IMAGE_MODEL_FILENAME = "vision_model.onnx"
TEXT_MODEL_FILENAME = "text_model.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
MANIFEST_FILENAME = "manifest.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export one immutable SigLIP2 image/text embedding space; runtime never downloads assets."
    )
    parser.add_argument("--model-id", default=APPROVED_MODEL_ID)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--max-length", type=int, default=64)
    return parser


def validate_export_request(model_id: str, revision: str, *, max_length: int = 64) -> None:
    if model_id != APPROVED_MODEL_ID:
        raise ValueError(f"model id must be {APPROVED_MODEL_ID}")
    if not revision.strip():
        raise ValueError("model revision must be non-empty")
    if max_length <= 0:
        raise ValueError("max length must be positive")


def build_joint_manifest(
    *,
    model_id: str,
    model_revision: str,
    dimension: int,
    image_model_file: str,
    image_sha256: str,
    image_external_data: dict[str, str],
    text_model_file: str,
    text_sha256: str,
    text_external_data: dict[str, str],
    tokenizer_file: str,
    tokenizer_sha256: str,
    max_length: int,
    image_size: int = 224,
    mean: tuple[float, float, float] = (0.5, 0.5, 0.5),
    std: tuple[float, float, float] = (0.5, 0.5, 0.5),
) -> dict[str, Any]:
    """Build schema v2 from two projections exported from the same model revision."""

    validate_export_request(model_id, model_revision, max_length=max_length)
    if dimension <= 0:
        raise ValueError("dimension must be positive")
    common_revision = model_revision.lower()
    return {
        "schema_version": 2,
        "model_id": model_id,
        "model_revision": common_revision,
        "dimension": dimension,
        "embedding_space_id": f"siglip2-base-p16-224@{common_revision}:joint-projection-v1",
        "supported_modalities": ["image", "text"],
        "image": {
            "model_revision": common_revision,
            "model_file": image_model_file,
            "model_sha256": image_sha256,
            "external_data": dict(sorted(image_external_data.items())),
            "projection": "visual_projection",
            "input_name": "pixel_values",
            "output_name": "image_embeds",
            "input_dtype": "float16",
            "preprocessing": {
                "size": image_size,
                "mean": list(mean),
                "std": list(std),
                "resample": "bicubic",
            },
        },
        "text": {
            "model_revision": common_revision,
            "model_file": text_model_file,
            "model_sha256": text_sha256,
            "external_data": dict(sorted(text_external_data.items())),
            "projection": "text_projection",
            "input_names": ["input_ids", "attention_mask"],
            "output_name": "text_embeds",
            "input_dtype": "int64",
            "tokenizer_file": tokenizer_file,
            "tokenizer_sha256": tokenizer_sha256,
            "preprocessing": {
                "max_length": max_length,
                "padding": "max_length",
                "truncation": True,
            },
        },
    }


def resolve_revision(model_id: str, requested_revision: str) -> str:
    if _looks_like_commit_sha(requested_revision):
        return requested_revision.lower()
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("export requires huggingface_hub") from exc
    resolved = str(HfApi().model_info(model_id, revision=requested_revision).sha or "").lower()
    if not _looks_like_commit_sha(resolved):
        raise RuntimeError("Hugging Face did not return an immutable model revision")
    return resolved


def export_joint_projections(
    *,
    model_id: str,
    model_revision: str,
    output_dir: Path,
    device_id: int,
    max_length: int,
) -> tuple[int, int, tuple[float, float, float], tuple[float, float, float]]:
    """Load one AutoModel and export both projected feature methods."""

    try:
        import torch
        from transformers import AutoImageProcessor, AutoModel, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("export requires torch and transformers") from exc
    if device_id < 0:
        raise ValueError("device id must be non-negative")
    if not torch.cuda.is_available():
        raise RuntimeError("FP16 SigLIP2 export requires an available CUDA device")
    device = torch.device(f"cuda:{device_id}")
    model = AutoModel.from_pretrained(
        model_id, revision=model_revision, torch_dtype=torch.float16
    ).eval().to(device)
    image_processor = AutoImageProcessor.from_pretrained(model_id, revision=model_revision)
    tokenizer = AutoTokenizer.from_pretrained(model_id, revision=model_revision, use_fast=True)

    class ImageProjection(torch.nn.Module):
        def __init__(self, source: Any) -> None:
            super().__init__()
            self.source = source

        def forward(self, pixel_values: Any) -> Any:
            return self.source.get_image_features(pixel_values=pixel_values)

    class TextProjection(torch.nn.Module):
        def __init__(self, source: Any) -> None:
            super().__init__()
            self.source = source

        def forward(self, input_ids: Any, attention_mask: Any) -> Any:
            return self.source.get_text_features(
                input_ids=input_ids, attention_mask=attention_mask
            )

    image_size = _processor_image_size(image_processor)
    mean = _three_floats(image_processor.image_mean, name="image_mean")
    std = _three_floats(image_processor.image_std, name="image_std")
    image_input = torch.zeros((1, 3, image_size, image_size), dtype=torch.float16, device=device)
    text_inputs = (
        torch.zeros((1, max_length), dtype=torch.int64, device=device),
        torch.ones((1, max_length), dtype=torch.int64, device=device),
    )
    image_wrapper = ImageProjection(model).eval()
    text_wrapper = TextProjection(model).eval()
    with torch.no_grad():
        image_dimension = int(image_wrapper(image_input).shape[-1])
        text_dimension = int(text_wrapper(*text_inputs).shape[-1])
    if image_dimension != text_dimension:
        raise RuntimeError("SigLIP2 image/text projection dimensions differ")
    torch.onnx.export(
        image_wrapper,
        (image_input,),
        str(output_dir / IMAGE_MODEL_FILENAME),
        input_names=["pixel_values"],
        output_names=["image_embeds"],
        opset_version=18,
        dynamo=True,
    )
    torch.onnx.export(
        text_wrapper,
        text_inputs,
        str(output_dir / TEXT_MODEL_FILENAME),
        input_names=["input_ids", "attention_mask"],
        output_names=["text_embeds"],
        opset_version=18,
        dynamo=True,
    )
    tokenizer.backend_tokenizer.save(str(output_dir / TOKENIZER_FILENAME))
    return image_dimension, image_size, mean, std


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    validate_export_request(args.model_id, args.revision, max_length=args.max_length)
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError("output directory already exists; refusing to overwrite")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    revision = resolve_revision(args.model_id, args.revision)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-export-", dir=output_dir.parent))
    try:
        dimension, image_size, mean, std = export_joint_projections(
            model_id=args.model_id,
            model_revision=revision,
            output_dir=temporary,
            device_id=args.device_id,
            max_length=args.max_length,
        )
        image_path = temporary / IMAGE_MODEL_FILENAME
        text_path = temporary / TEXT_MODEL_FILENAME
        manifest = build_joint_manifest(
            model_id=args.model_id,
            model_revision=revision,
            dimension=dimension,
            image_model_file=IMAGE_MODEL_FILENAME,
            image_sha256=sha256_file(image_path),
            image_external_data=_external_checksums(temporary, image_path),
            text_model_file=TEXT_MODEL_FILENAME,
            text_sha256=sha256_file(text_path),
            text_external_data=_external_checksums(temporary, text_path),
            tokenizer_file=TOKENIZER_FILENAME,
            tokenizer_sha256=sha256_file(temporary / TOKENIZER_FILENAME),
            max_length=args.max_length,
            image_size=image_size,
            mean=mean,
            std=std,
        )
        (temporary / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "status": "exported",
        "model_id": args.model_id,
        "model_revision": revision,
        "output_dir": str(output_dir),
        "dimension": dimension,
        "supported_modalities": ["image", "text"],
    }


def _external_checksums(root: Path, model_path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for location in onnx_external_data_locations(model_path):
        path = (root / location).resolve()
        if root not in path.parents or not path.is_file():
            raise RuntimeError("exported ONNX external data is invalid or missing")
        checksums[location] = sha256_file(path)
    return checksums


def onnx_external_data_locations(model_path: Path) -> set[str]:
    try:
        import onnx
    except ImportError as exc:
        raise RuntimeError("export requires onnx") from exc
    model = onnx.load_model(str(model_path), load_external_data=False)
    locations: set[str] = set()
    for tensor in model.graph.initializer:
        location = {entry.key: entry.value for entry in tensor.external_data}.get("location")
        if location:
            locations.add(str(location))
    return locations


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_export(args)
    except (FileExistsError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _looks_like_commit_sha(value: str) -> bool:
    normalized = value.strip().lower()
    return len(normalized) == 40 and all(c in "0123456789abcdef" for c in normalized)


def _processor_image_size(processor: Any) -> int:
    size = processor.size
    if isinstance(size, dict):
        height, width = int(size.get("height") or 0), int(size.get("width") or 0)
        if height == width and height > 0:
            return height
    if isinstance(size, int) and size > 0:
        return size
    raise RuntimeError("SigLIP2 processor does not define one square image size")


def _three_floats(value: Any, *, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise RuntimeError(f"SigLIP2 processor {name} must contain three values")
    result = tuple(float(item) for item in value)
    return result[0], result[1], result[2]


if __name__ == "__main__":
    raise SystemExit(main())

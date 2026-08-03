#!/usr/bin/env python3
"""Explicitly export the approved SigLIP2 image projection branch to ONNX."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence


APPROVED_MODEL_ID = "google/siglip2-base-patch16-224"
MODEL_FILENAME = "vision_model.onnx"
MANIFEST_FILENAME = "manifest.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export only the SigLIP2 image encoder plus visual projection. "
            "The Runtime never downloads models."
        )
    )
    parser.add_argument("--model-id", default=APPROVED_MODEL_ID)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device-id", type=int, default=0)
    return parser


def validate_export_request(model_id: str, revision: str) -> None:
    """Restrict the operator entry point to the reviewed model family."""

    if model_id != APPROVED_MODEL_ID:
        raise ValueError(f"model id must be {APPROVED_MODEL_ID}")
    if not revision.strip():
        raise ValueError("model revision must be non-empty")


def build_manifest(
    *,
    model_id: str,
    model_revision: str,
    model_file: str,
    model_sha256: str,
    dimension: int,
    image_size: int,
    mean: tuple[float, float, float],
    std: tuple[float, float, float],
) -> dict[str, Any]:
    """Build the runtime identity contract for the image-projection graph."""

    validate_export_request(model_id, model_revision)
    return {
        "schema_version": 1,
        "model_id": model_id,
        "model_revision": model_revision,
        "model_file": model_file,
        "model_sha256": model_sha256,
        "dimension": dimension,
        "embedding_space_id": (
            f"siglip2-base-p16-224@{model_revision}:image-projection-v1"
        ),
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
    }


def resolve_revision(model_id: str, requested_revision: str) -> str:
    """Resolve a branch/tag to the immutable Hub commit recorded in the manifest."""

    if _looks_like_commit_sha(requested_revision):
        return requested_revision.lower()
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError(
            "export requires huggingface_hub from the transformers toolchain"
        ) from exc
    info = HfApi().model_info(model_id, revision=requested_revision)
    resolved = str(info.sha or "").strip().lower()
    if not _looks_like_commit_sha(resolved):
        raise RuntimeError("Hugging Face did not return an immutable model revision")
    return resolved


def export_image_projection(
    *,
    model_id: str,
    model_revision: str,
    output_path: Path,
    device_id: int,
) -> tuple[int, int, tuple[float, float, float], tuple[float, float, float]]:
    """Export `get_image_features` so the ONNX output matches future text features."""

    try:
        import torch
        from transformers import AutoImageProcessor, AutoModel
    except ImportError as exc:
        raise RuntimeError(
            "export requires torch and transformers; install export-only dependencies"
        ) from exc
    if device_id < 0:
        raise ValueError("device id must be non-negative")
    if not torch.cuda.is_available():
        raise RuntimeError("FP16 SigLIP2 export requires an available CUDA device")

    device = torch.device(f"cuda:{device_id}")
    processor = AutoImageProcessor.from_pretrained(
        model_id,
        revision=model_revision,
    )
    model = AutoModel.from_pretrained(
        model_id,
        revision=model_revision,
        torch_dtype=torch.float16,
    ).eval().to(device)

    class ImageProjection(torch.nn.Module):
        def __init__(self, source_model: Any) -> None:
            super().__init__()
            self.source_model = source_model

        def forward(self, pixel_values: Any) -> Any:
            return self.source_model.get_image_features(pixel_values=pixel_values)

    image_size = _processor_image_size(processor)
    mean = _three_floats(processor.image_mean, name="image_mean")
    std = _three_floats(processor.image_std, name="image_std")
    wrapper = ImageProjection(model).eval()
    dummy = torch.zeros(
        (1, 3, image_size, image_size),
        dtype=torch.float16,
        device=device,
    )
    with torch.no_grad():
        dimension = int(wrapper(dummy).shape[-1])
    torch.onnx.export(
        wrapper,
        (dummy,),
        str(output_path),
        input_names=["pixel_values"],
        output_names=["image_embeds"],
        opset_version=18,
        dynamo=True,
    )
    return dimension, image_size, mean, std


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    """Create one immutable runtime directory without overwriting prior assets."""

    validate_export_request(args.model_id, args.revision)
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError("output directory already exists; refusing to overwrite")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    resolved_revision = resolve_revision(args.model_id, args.revision)

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-export-",
            dir=output_dir.parent,
        )
    )
    try:
        model_path = temporary / MODEL_FILENAME
        dimension, image_size, mean, std = export_image_projection(
            model_id=args.model_id,
            model_revision=resolved_revision,
            output_path=model_path,
            device_id=args.device_id,
        )
        model_sha256 = sha256_file(model_path)
        manifest = build_manifest(
            model_id=args.model_id,
            model_revision=resolved_revision,
            model_file=MODEL_FILENAME,
            model_sha256=model_sha256,
            dimension=dimension,
            image_size=image_size,
            mean=mean,
            std=std,
        )
        (temporary / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "status": "exported",
        "model_id": args.model_id,
        "model_revision": resolved_revision,
        "output_dir": str(output_dir),
        "dimension": dimension,
    }


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
    return len(normalized) == 40 and all(
        character in "0123456789abcdef" for character in normalized
    )


def _processor_image_size(processor: Any) -> int:
    size = processor.size
    if isinstance(size, dict):
        height = int(size.get("height") or 0)
        width = int(size.get("width") or 0)
        if height == width and height > 0:
            return height
    if isinstance(size, int) and size > 0:
        return size
    raise RuntimeError("SigLIP2 processor does not define one square image size")


def _three_floats(value: Any, *, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise RuntimeError(f"SigLIP2 processor {name} must contain three values")
    converted = tuple(float(item) for item in value)
    return converted[0], converted[1], converted[2]


if __name__ == "__main__":
    raise SystemExit(main())

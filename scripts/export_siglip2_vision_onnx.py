#!/usr/bin/env python3
"""Deprecated compatibility entry for the joint SigLIP2 exporter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


def _joint_module():
    name = "assistant_agent_siglip2_joint_export"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).with_name("export_siglip2_embedding_onnx.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("joint SigLIP2 exporter is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    external_data: dict[str, str],
) -> dict[str, Any]:
    """Build the schema-v1 image-only manifest for legacy tooling tests."""

    validate_export_request(model_id, model_revision)
    return {
        "schema_version": 1,
        "model_id": model_id,
        "model_revision": model_revision,
        "model_file": model_file,
        "model_sha256": model_sha256,
        "dimension": dimension,
        "embedding_space_id": f"siglip2-base-p16-224@{model_revision}:image-projection-v1",
        "projection": "visual_projection",
        "input_name": "pixel_values",
        "output_name": "image_embeds",
        "input_dtype": "float16",
        "external_data": dict(sorted(external_data.items())),
        "preprocessing": {
            "size": image_size,
            "mean": list(mean),
            "std": list(std),
            "resample": "bicubic",
        },
    }


def validate_export_request(model_id: str, revision: str) -> None:
    _joint_module().validate_export_request(model_id, revision)


def onnx_external_data_locations(model_path: Path) -> set[str]:
    return _joint_module().onnx_external_data_locations(model_path)


def build_parser():
    return _joint_module().build_parser()


def main(argv: Sequence[str] | None = None) -> int:
    print(
        "deprecated: use scripts/export_siglip2_embedding_onnx.py; exporting joint image/text assets",
        file=sys.stderr,
    )
    return _joint_module().main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

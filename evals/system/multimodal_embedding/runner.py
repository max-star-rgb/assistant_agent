"""Operator-gated joint SigLIP2 system eval with content-free artifacts."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from assistant_agent.media.embedding.comparator import EmbeddingComparator
from assistant_agent.media.embedding.local_siglip2 import (
    LocalSiglip2EmbeddingConfig,
    LocalSiglip2EmbeddingProvider,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation, TextObservation


DEFAULT_OUTPUT_ROOT = Path(".data/evals/system/multimodal_embedding")


def dry_run_report(model_dir: Path | None) -> dict[str, object]:
    return {
        "status": "dry_run",
        "model_dir_configured": model_dir is not None,
        "model_dir_exists": bool(model_dir and model_dir.expanduser().is_dir()),
        "would_check": [
            "image_text_readiness",
            "shared_embedding_space",
            "fixed_input_repeatability",
            "positive_negative_ranking",
            "raw_frame_siglip2_latest_pending",
            "one_fps_keyframe_fallback",
            "semantic_keyframe_selection",
            "selected_keyframe_parallel_vlm",
            "vlm_text_indexing",
            "visual_memory_text_ranking",
            "visual_memory_query_without_vlm",
            "cuda_first_provider",
            "cpu_fallback_disabled",
        ],
        "local_model_loaded": False,
    }


def run_local_model_eval(
    *,
    model_dir: Path,
    cuda_device_id: int = 0,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> tuple[Path, dict[str, object]]:
    provider = LocalSiglip2EmbeddingProvider(
        LocalSiglip2EmbeddingConfig(
            model_dir=model_dir,
            cuda_device_id=cuda_device_id,
        )
    )
    readiness = provider.readiness()
    if not readiness.image_ready or not readiness.text_ready:
        raise RuntimeError("joint SigLIP2 image/text assets are not ready")
    comparator = EmbeddingComparator()
    with tempfile.TemporaryDirectory(prefix="siglip2-system-eval-") as temporary:
        from PIL import Image

        temp_root = Path(temporary)
        positive_path = temp_root / "positive.jpg"
        negative_path = temp_root / "negative.jpg"
        Image.new("RGB", (224, 224), color=(255, 255, 255)).save(positive_path)
        Image.new("RGB", (224, 224), color=(0, 0, 0)).save(negative_path)
        positive_observation = ImageObservation(
            session_id="system-eval",
            observation_id="positive-image",
            image_ref=str(positive_path),
        )
        first = provider.embed_image(positive_observation)
        repeated = provider.embed_image(positive_observation)
        negative = provider.embed_image(
            ImageObservation(
                session_id="system-eval",
                observation_id="negative-image",
                image_ref=str(negative_path),
            )
        )
        query = provider.embed_text(
            TextObservation(
                session_id="system-eval",
                observation_id="positive-query",
                text="a plain white square",
                source="system_eval",
            )
        )
    outcomes = [first, repeated, negative, query]
    if not all(isinstance(item, EmbeddingEvent) for item in outcomes):
        codes = [getattr(item, "code", "unknown") for item in outcomes]
        raise RuntimeError(f"local embedding inference failed: {codes}")
    assert isinstance(first, EmbeddingEvent)
    assert isinstance(repeated, EmbeddingEvent)
    assert isinstance(negative, EmbeddingEvent)
    assert isinstance(query, EmbeddingEvent)
    positive_similarity = comparator.similarity(query, first)
    negative_similarity = comparator.similarity(query, negative)
    backend = provider._backend
    image_providers = backend._image_session.get_providers() if backend else []
    text_providers = backend._text_session.get_providers() if backend else []
    checks = {
        "image_text_ready": readiness.image_ready and readiness.text_ready,
        "shared_embedding_space": len(
            {first.embedding_space_id, negative.embedding_space_id, query.embedding_space_id}
        )
        == 1,
        "fixed_input_repeatable": first.vector == repeated.vector,
        "positive_ranks_above_negative": positive_similarity > negative_similarity,
        "cuda_first_provider": bool(image_providers and text_providers)
        and image_providers[0] == text_providers[0] == "CUDAExecutionProvider",
        "cpu_fallback_disabled": bool(
            backend and getattr(backend, "cpu_fallback_disabled", False)
        ),
    }
    result: dict[str, object] = {
        "status": "passed" if all(checks.values()) else "failed",
        "passed": all(checks.values()),
        "checks": checks,
        "model_revision_digest": _digest(readiness.model_revision),
        "embedding_space_id_digest": _digest(readiness.embedding_space_id),
        "dimension": readiness.dimension,
        "positive_similarity": positive_similarity,
        "negative_similarity": negative_similarity,
    }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, result


def _digest(value: str | None) -> str | None:
    if value is None:
        return None
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

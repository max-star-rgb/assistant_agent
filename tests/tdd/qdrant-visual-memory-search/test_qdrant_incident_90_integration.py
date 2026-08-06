from __future__ import annotations

import os
from uuid import uuid4

import pytest

from assistant_agent.media.video.qdrant_visual_memory_index import (
    FastEmbedDenseTextEncoder,
    QdrantHttpTransport,
    QdrantTransportError,
    QdrantVisualMemoryTextIndex,
)
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexDocument,
    VisualMemoryIndexQuery,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_VISUAL_MEMORY_QDRANT_INTEGRATION") != "1",
    reason="requires an operator-started local Qdrant and prefilled BGE cache",
)


MOUSE_TEXTS = {
    3: "白色桌面旁边放着一个白色鼠标。",
    15: "桌面右侧可以看到一个黑色鼠标。",
    46: "键盘旁出现一只鼠标。",
    50: "黑色鼠标位于桌面上。",
    80: "画面边缘出现深色鼠标。",
    81: "画面中可见一只罗技鼠标，位于键盘右侧。",
    82: "罗技鼠标仍然清晰可见。",
    83: "桌上的罗技鼠标位于画面中央。",
    84: "当前画面主要物体是一只罗技鼠标。",
    85: "近处可见黑色罗技鼠标。",
}


def test_incident_shape_mouse_query_recalls_seq_81_to_85_in_top_three() -> None:
    transport = QdrantHttpTransport(
        base_url=os.environ.get(
            "VISUAL_MEMORY_QDRANT_URL",
            "http://127.0.0.1:6333",
        ),
        timeout_seconds=5.0,
    )
    collection = f"visual_memory_incident_{uuid4().hex}"
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FastEmbedDenseTextEncoder(
            cache_dir=os.environ.get(
                "VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR",
                ".data/models/fastembed",
            )
        ),
        collection_name=collection,
    )
    try:
        for sequence in range(1, 91):
            outcome = index.upsert(
                VisualMemoryIndexDocument(
                    record_id=f"record-{sequence}",
                    user_id="incident-user",
                    session_id="incident-session",
                    video_id="incident-video",
                    frame_sequence=sequence,
                    captured_at_ms=1_754_469_384_000 + sequence * 1_000,
                    text=MOUSE_TEXTS.get(
                        sequence,
                        f"第{sequence}帧展示普通桌面、线缆和显示器。",
                    ),
                )
            )
            assert outcome.status == "ready", (
                sequence,
                outcome.model_dump(mode="json"),
            )

        result = index.search(
            VisualMemoryIndexQuery(
                user_id="incident-user",
                session_id="incident-session",
                query="鼠标",
                freshness_record_id="record-90",
                limit=12,
            )
        )

        top_three = [hit.document.frame_sequence for hit in result.hits[:3]]
        assert set(top_three) & {81, 82, 83, 84, 85}
    finally:
        try:
            transport.request(
                "DELETE",
                f"/collections/{collection}",
                timeout_seconds=60.0,
            )
        except QdrantTransportError:
            pass

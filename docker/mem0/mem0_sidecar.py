"""Minimal REST-only Mem0 OSS sidecar; it does not expose an Agent runtime."""

from __future__ import annotations

import os
from threading import Lock
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from qdrant_client import QdrantClient

os.environ.setdefault("MEM0_TELEMETRY", "False")

from mem0 import Memory
from mem0_env import (
    LONG_TERM_MEMORY_CUSTOM_INSTRUCTIONS,
    clear_memories,
    collect_all_memories,
    list_unfiltered_memories,
    resolve_mem0_provider_environment,
)


app = FastAPI(title="assistant_agent Mem0 sidecar", version="2.0.11")
_MEMORY: Memory | None = None
_MEMORY_LOCK = Lock()


def memory() -> Memory:
    global _MEMORY
    if _MEMORY is None:
        with _MEMORY_LOCK:
            if _MEMORY is None:
                _MEMORY = _build_memory()
    return _MEMORY


def _build_memory() -> Memory:
    provider_env = resolve_mem0_provider_environment()
    return Memory.from_config(
        {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": provider_env["chat_model"],
                    "api_key": provider_env["chat_api_key"],
                    "openai_base_url": provider_env["chat_base_url"],
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": provider_env["embedding_model"],
                    "api_key": provider_env["embedding_api_key"],
                    "openai_base_url": provider_env["embedding_base_url"],
                    "embedding_dims": 1024,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "client": _qdrant_client(),
                    "collection_name": "assistant_agent_memory",
                    "embedding_model_dims": 1024,
                },
            },
            "custom_instructions": LONG_TERM_MEMORY_CUSTOM_INSTRUCTIONS,
            "history_db_path": os.getenv("HISTORY_DB_PATH", "/data/history/history.db"),
        }
    )


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        _ = memory()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory not ready") from exc
    return {
        "status": "ok",
        "framework": "mem0",
        "version": "2.0.11",
        "ready": True,
    }


@app.get("/")
@app.get("/ready")
def ready() -> dict[str, Any]:
    _ = memory()
    return {"status": "ok", "framework": "mem0", "version": "2.0.11"}


@app.post("/memories")
def add_memories(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result = memory().add(
        payload.get("messages") or payload.get("memory") or "",
        user_id=payload.get("user_id"),
        agent_id=payload.get("agent_id"),
        run_id=payload.get("run_id"),
        metadata=payload.get("metadata") or {},
        infer=bool(payload.get("infer", True)),
    )
    return _result(result)


@app.post("/search")
def search_memories(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result = memory().search(
        query=str(payload.get("query") or ""),
        filters=payload.get("filters") or _entity_filters(payload),
        limit=int(payload.get("top_k") or payload.get("limit") or 5),
    )
    return _result(result)


@app.get("/memories")
def list_memories(
    user_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=50),
) -> dict[str, Any]:
    filters = _entity_filters(locals())
    if not filters:
        return {
            "results": list_unfiltered_memories(memory(), limit=limit),
        }
    if limit is not None:
        results = _result(
            memory().get_all(filters=filters, top_k=limit)
        ).get("results", [])
        return {"results": results}
    return {
        "results": collect_all_memories(
            lambda top_k: _result(
                memory().get_all(filters=filters, top_k=top_k)
            ).get("results", [])
        )
    }


@app.get("/memories/{memory_id}")
def get_memory(memory_id: str) -> dict[str, Any]:
    result = memory().get(memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return _mapping(result)


@app.get("/memories/{memory_id}/history")
def memory_history(memory_id: str) -> dict[str, Any]:
    return {"history": memory().history(memory_id)}


@app.put("/memories/{memory_id}")
def update_memory(memory_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _mapping(memory().update(memory_id, data=str(payload.get("memory") or payload.get("text") or "")))


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str) -> dict[str, Any]:
    try:
        memory().delete(memory_id)
    except ValueError as exc:
        if "not found" not in str(exc).lower():
            raise
        return {"success": True, "deleted": False}
    return {"success": True, "deleted": True}


@app.delete("/memories")
def clear_memory_records(
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        return clear_memories(memory(), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _qdrant_client() -> QdrantClient:
    return QdrantClient(
        host=os.getenv("QDRANT_HOST", "qdrant"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
        timeout=float(os.getenv("QDRANT_TIMEOUT_SECONDS", "30")),
    )


def _entity_filters(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(payload[key])
        for key in ("user_id", "agent_id", "run_id")
        if payload.get(key)
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {"result": value}


def _result(value: Any) -> dict[str, Any]:
    mapped = _mapping(value)
    if "results" in mapped:
        return mapped
    if isinstance(value, list):
        return {"results": value}
    return mapped

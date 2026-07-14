"""Minimal REST-only Mem0 OSS sidecar; it does not expose an Agent runtime."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from mem0 import Memory


app = FastAPI(title="assistant_agent Mem0 sidecar", version="2.0.11")


@lru_cache(maxsize=1)
def memory() -> Memory:
    return Memory.from_config(
        {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": _required("OPENAI_MODEL"),
                    "api_key": _required("OPENAI_API_KEY"),
                    "openai_base_url": _required("OPENAI_BASE_URL"),
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": _required("EMBEDDING_MODEL"),
                    "api_key": _required("EMBEDDING_API_KEY"),
                    "openai_base_url": _required("EMBEDDING_BASE_URL"),
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": os.getenv("QDRANT_HOST", "qdrant"),
                    "port": int(os.getenv("QDRANT_PORT", "6333")),
                    "collection_name": "assistant_agent_memory_bakeoff",
                },
            },
            "history_db_path": os.getenv("HISTORY_DB_PATH", "/data/history/history.db"),
        }
    )


@app.get("/")
def health() -> dict[str, Any]:
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
        top_k=int(payload.get("top_k") or payload.get("limit") or 5),
    )
    return _result(result)


@app.get("/memories")
def list_memories(
    user_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    run_id: str | None = Query(None),
) -> dict[str, Any]:
    return _result(memory().get_all(filters=_entity_filters(locals())))


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
    memory().delete(memory_id)
    return {"success": True, "deleted": True}


@app.delete("/memories")
def clear_memories(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    filters = _entity_filters(payload)
    before = _result(memory().get_all(filters=filters)).get("results", [])
    memory().delete_all(**filters)
    return {"success": True, "deleted_count": len(before)}


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required sidecar environment variable is missing: {name}")
    return value


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

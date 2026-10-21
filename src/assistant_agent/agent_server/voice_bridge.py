"""Authenticated Qwen bridge for shared search."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from langchain_core.utils.function_calling import convert_to_openai_tool
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assistant_agent.agent_server.browser_media import _require_local

router = APIRouter(prefix="/qwen-audio")
Identifier = Annotated[str, Field(min_length=1, max_length=160)]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Identifier


class SearchInput(SessionInput):
    arguments: dict


def execution_owner():
    from assistant_agent.agent_server.graph import current_native_execution_owner
    return current_native_execution_owner()


def authenticated_identity(request: Request):
    _require_local(request)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "same-origin required")
    return str(request.scope["user"].identity)


def authenticated_owner(request: Request):
    identity = authenticated_identity(request)
    return execution_owner(), identity


@router.get("/capabilities")
async def capabilities(request: Request):
    owner, _ = authenticated_owner(request)
    return {
        "search_tool": convert_to_openai_tool(owner.voice_search_tool) if owner.voice_search_tool is not None else None,
    }


@router.post("/search")
async def search(payload: SearchInput, request: Request):
    owner, _ = authenticated_owner(request)
    if owner.voice_search_tool is None:
        raise HTTPException(409, "bailian search unavailable")
    try:
        with tracing_context(enabled=False):
            return await owner.voice_search_tool.ainvoke(payload.arguments)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_search_arguments") from exc
    except Exception as exc:
        raise HTTPException(502, "bailian_search_failed") from exc


@router.get("/visual-memory/snapshot")
@router.post("/visual-memory/snapshot")
async def visual_memory_snapshot(request: Request):
    identity = authenticated_identity(request)
    snapshots = getattr(request.app.state, "visual_memory_snapshots", None)
    if snapshots is None:
        return {"enabled": False, "texts": [], "status": "disabled"}
    if request.method == "POST":
        await snapshots.initialize(identity)
    return snapshots.read(identity)

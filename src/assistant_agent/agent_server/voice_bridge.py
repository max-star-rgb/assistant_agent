"""Local authenticated Qwen bridge to shared Voice search and Memory services."""

from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from assistant_agent.agent_server.browser_media import _require_local
from assistant_agent.agent_server.voice_tasks import VoiceTasks
from assistant_agent.memory.edit_models import MemoryRequest, MEMORY_DESCRIPTION

router = APIRouter(prefix="/qwen-audio")
Text = Annotated[str, Field(min_length=1, max_length=16000)]
Identifier = Annotated[str, Field(min_length=1, max_length=160)]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Identifier


class RecallInput(SessionInput):
    request: Text


class MemoryReadInput(SessionInput):
    query: str | None = Field(default=None, max_length=16000)


class SearchInput(SessionInput):
    arguments: dict


class TranscriptItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    text: Text


class TranscriptInput(SessionInput):
    items: list[TranscriptItem] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def bounded_snapshot(self):
        if sum(len(item.text) for item in self.items) > 64000:
            raise ValueError("transcript snapshot exceeds budget")
        return self


def execution_owner():
    from assistant_agent.agent_server.graph import current_native_execution_owner

    return current_native_execution_owner()


def authenticated_owner(request: Request):
    _require_local(request)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "same-origin required")
    return execution_owner(), str(request.scope["user"].identity)


def memory_enabled(owner):
    return owner.memory_backend is not None and owner.memory_backend.backend_id != "disabled"


def voice_tasks(owner, identity: str, session_id: str):
    source = str(uuid5(NAMESPACE_URL, f"assistant-agent:qwen-memory:{identity}:{session_id}"))
    tasks = VoiceTasks(None, source, memory_backend=owner.memory_backend, identity=identity)
    tasks.memory_store = owner.memory_store
    return tasks


@router.get("/capabilities")
async def capabilities(request: Request):
    owner, _ = authenticated_owner(request)
    return {"memory": memory_enabled(owner), 'memory_editable': getattr(owner.memory_backend, 'supports_edits', False),
            'memory_tool': {'type': 'function', 'function': {'name': 'recall_memory',
                'description': MEMORY_DESCRIPTION, 'parameters': MemoryRequest.model_json_schema()}}
                if getattr(owner.memory_backend, 'supports_edits', False) else None, "search_tool": (
        convert_to_openai_tool(owner.voice_search_tool) if owner.voice_search_tool is not None else None
    )}


@router.post('/memory/read')
async def read_memory(payload: MemoryReadInput, request: Request):
    owner, identity = authenticated_owner(request)
    if not memory_enabled(owner):
        raise HTTPException(409, 'memory disabled')
    try:
        tasks = voice_tasks(owner, identity, payload.session_id)
        reader = getattr(owner.memory_backend, 'read', None)
        if reader:
            return await reader(identity=identity, thread_id=tasks.session_id, query=payload.query)
        if payload.query:
            return await tasks.call('recall_memory', {'request': payload.query})
        raise HTTPException(409, 'memory_snapshot_unsupported')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'memory_read_failed') from exc


@router.post('/memory/session')
async def begin_memory_session(payload: SessionInput, request: Request):
    owner, identity = authenticated_owner(request)
    if getattr(owner.memory_backend, 'supports_edits', False):
        tasks = voice_tasks(owner, identity, payload.session_id)
        await owner.memory_backend.prepare_extraction(identity=identity, thread_id=tasks.session_id, messages=[])
    return {'status': 'ready'}


@router.post("/search")
async def search(payload: SearchInput, request: Request):
    owner, _ = authenticated_owner(request)
    if owner.voice_search_tool is None:
        raise HTTPException(409, "bailian search unavailable")
    try:
        return await owner.voice_search_tool.ainvoke(payload.arguments)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_search_arguments") from exc
    except Exception as exc:
        raise HTTPException(502, "bailian_search_failed") from exc


@router.post("/recall")
async def recall(payload: RecallInput, request: Request):
    owner, identity = authenticated_owner(request)
    if not memory_enabled(owner):
        raise HTTPException(409, "memory disabled")
    try:
        return await voice_tasks(owner, identity, payload.session_id).call("recall_memory", {"request": payload.request})
    except Exception as exc:
        raise HTTPException(502, "memory_recall_failed") from exc


@router.post("/transcripts")
async def transcripts(payload: TranscriptInput, request: Request):
    owner, identity = authenticated_owner(request)
    if not memory_enabled(owner):
        return {"status": "disabled"}
    tasks = voice_tasks(owner, identity, payload.session_id)
    tasks.transcripts = {item.id: item.text for item in payload.items}
    try:
        await tasks.refresh_memory()
    except Exception as exc:
        raise HTTPException(502, "memory_submission_failed") from exc
    return {"status": "submitted"}

"""Strict domain values used by the native planning graph."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


ProviderSearchProfile = Literal[
    "none",
    "rail_official",
    "flight_official",
    "guide_official",
    "guide_xiaohongshu",
    "travel_general",
]


class PlanningTodo(BaseModel):
    """One item in the Supervisor's explicit planning working memory."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    todo_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    content: str = Field(min_length=1, max_length=4_000)
    status: Literal["pending", "completed"] = "pending"


class WorkerResult(BaseModel):
    """Normal business outcome returned by one planning Worker invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    todo_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    status: Literal["succeeded", "blocked"]
    summary: str = Field(min_length=1, max_length=100_000)


class WorkerResultSchema(TypedDict):
    """JSON-safe structured-output schema used inside create_agent state."""

    todo_id: Annotated[
        str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    ]
    status: Literal["succeeded", "blocked"]
    summary: Annotated[str, Field(min_length=1, max_length=100_000)]


# LangChain uses the schema type name as the structured Tool name. Keep the
# public protocol name stable while the checkpoint payload itself stays a dict.
WorkerResultSchema.__name__ = "WorkerResult"


class WorkerWrite(BaseModel):
    """One fan-out branch write waiting for the deterministic join."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_call_id: str = Field(min_length=1, max_length=240)
    result: WorkerResult


__all__ = [
    "PlanningTodo",
    "ProviderSearchProfile",
    "WorkerResult",
    "WorkerResultSchema",
    "WorkerWrite",
]

"""Contracts for governed local text-file access."""

from typing import Literal

from pydantic import BaseModel, Field


class FileReadRequest(BaseModel):
    """Model-visible file selection plus runtime-owned read limits."""

    path: str = Field(
        min_length=1,
        max_length=1_024,
        description="配置根目录下的相对路径。",
    )
    cursor: int = Field(
        default=0,
        ge=0,
        description="续读使用上次返回的 next_cursor。",
    )
    max_chars: int = Field(default=12_000, ge=1, le=50_000)


class FileReadError(BaseModel):
    """Stable, assistant-safe local file error."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class FileReadResult(BaseModel):
    """One bounded page of decoded local text."""

    status: Literal["succeeded", "failed"]
    path: str
    content: str = ""
    encoding: str | None = None
    start_char: int = Field(default=0, ge=0)
    end_char: int = Field(default=0, ge=0)
    total_chars: int = Field(default=0, ge=0)
    truncated: bool = False
    next_cursor: int | None = Field(default=None, ge=0)
    errors: list[FileReadError] = Field(default_factory=list)

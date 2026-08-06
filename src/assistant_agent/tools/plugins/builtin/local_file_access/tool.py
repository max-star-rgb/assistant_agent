"""Governed, root-scoped local text-file reader Tool."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from assistant_agent.tools.plugins.builtin.local_file_access.models import (
    FileReadError,
    FileReadRequest,
    FileReadResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.base import (
    ToolBase,
    ToolContext,
    ToolInputValidationError,
)


LOCAL_FILE_READ_TOOL_NAME: Final = "file_read"
DEFAULT_MAX_FILE_BYTES: Final = 2 * 1024 * 1024
SUPPORTED_TEXT_SUFFIXES: Final = frozenset(
    {
        ".cfg",
        ".csv",
        ".go",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".log",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class LocalFileReadTool(ToolBase):
    name = LOCAL_FILE_READ_TOOL_NAME
    description = "读取配置根目录内的文本文件，支持分页。"
    input_schema = FileReadRequest
    output_schema = FileReadResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("max_chars",)

    def __init__(
        self,
        *,
        root: str | Path,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_file_bytes = max_file_bytes

    def validate_call(self, input: FileReadRequest) -> None:
        _validated_relative_path(input.path)

    def _run(self, input: FileReadRequest, context: ToolContext) -> ToolResult:
        try:
            relative_path = _validated_relative_path(input.path)
            resolved_path = (self.root / relative_path).resolve(strict=True)
            resolved_path.relative_to(self.root)
        except ToolInputValidationError as exc:
            return _failure(self.name, input.path, exc.code, exc.message)
        except FileNotFoundError:
            return _failure(
                self.name,
                input.path,
                "file_not_found",
                "指定文件不存在。",
            )
        except (OSError, RuntimeError, ValueError):
            return _failure(
                self.name,
                input.path,
                "file_access_denied",
                "指定路径不在允许读取的文件根目录内。",
            )

        try:
            stat_result = resolved_path.stat()
            if not resolved_path.is_file():
                return _failure(
                    self.name,
                    input.path,
                    "file_not_regular",
                    "指定路径不是普通文件。",
                )
            if stat_result.st_size > self.max_file_bytes:
                return _failure(
                    self.name,
                    input.path,
                    "file_too_large",
                    f"文件超过允许的 {self.max_file_bytes} 字节上限。",
                )
            raw = resolved_path.read_bytes()
            if len(raw) > self.max_file_bytes:
                return _failure(
                    self.name,
                    input.path,
                    "file_too_large",
                    f"文件超过允许的 {self.max_file_bytes} 字节上限。",
                )
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _failure(
                self.name,
                input.path,
                "file_encoding_unsupported",
                "文件不是受支持的 UTF-8 文本。",
            )
        except OSError:
            return _failure(
                self.name,
                input.path,
                "file_read_failed",
                "文件读取失败。",
            )

        if input.cursor > len(text):
            return _failure(
                self.name,
                input.path,
                "file_cursor_invalid",
                "cursor 超出文件文本范围。",
            )

        end_char = min(len(text), input.cursor + input.max_chars)
        content = text[input.cursor:end_char]
        truncated = end_char < len(text)
        result = FileReadResult(
            status="succeeded",
            path=relative_path.as_posix(),
            content=content,
            encoding="utf-8",
            start_char=input.cursor,
            end_char=end_char,
            total_chars=len(text),
            truncated=truncated,
            next_cursor=end_char if truncated else None,
        )
        data = result.model_dump(mode="json")
        summary = (
            f"已读取 {result.path} 的字符 {result.start_char}-{result.end_char}，"
            f"全文共 {result.total_chars} 字符。"
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation={
                "summary": summary,
                "path": result.path,
                "content": result.content,
                "start_char": result.start_char,
                "end_char": result.end_char,
                "total_chars": result.total_chars,
                "truncated": result.truncated,
                "next_cursor": result.next_cursor,
            },
            trace_summary={
                "status": result.status,
                "path": result.path,
                "returned_chars": len(result.content),
                "total_chars": result.total_chars,
                "truncated": result.truncated,
            },
            audit_payload={
                "path": result.path,
                "content_redacted": True,
            },
        )


def _validated_relative_path(raw_path: str) -> Path:
    normalized = raw_path.strip()
    path = Path(normalized)
    if not normalized or path.is_absolute():
        raise ToolInputValidationError(
            "file_path_invalid",
            "path 必须是文件根目录内的非空相对路径。",
        )
    if any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        raise ToolInputValidationError(
            "file_path_invalid",
            "path 不能包含隐藏路径或目录穿越片段。",
        )
    if path.suffix.lower() not in SUPPORTED_TEXT_SUFFIXES:
        raise ToolInputValidationError(
            "file_type_unsupported",
            "当前只支持白名单中的文本文件类型。",
        )
    return path


def _failure(
    tool_name: str,
    path: str,
    code: str,
    message: str,
) -> ToolResult:
    result = FileReadResult(
        status="failed",
        path=path,
        errors=[FileReadError(code=code, message=message)],
    )
    data = result.model_dump(mode="json")
    return ToolResult(
        tool_name=tool_name,
        success=False,
        data=data,
        model_observation={
            "summary": message,
            "path": path,
            "errors": data["errors"],
        },
        trace_summary={
            "status": result.status,
            "error_code": code,
        },
        audit_payload={
            "path": path,
            "content_redacted": True,
        },
        error=f"{code}: {message}",
    )

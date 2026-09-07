"""Layered Assistant system prompt assembly."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

from assistant_agent.media.runtime_media import (
    RuntimeMediaSnapshot,
    latest_runtime_media,
)
from assistant_agent.native_agent.context import (
    AssistantRunContext,
)
from assistant_agent.native_agent.prompts.media import (
    MEDIA_OUTPUT_TOKEN_LIMIT,
    render_media_prompt,
)
from assistant_agent.native_agent.prompts.studio import render_studio_prompt
from assistant_agent.native_agent.user_context import (
    render_user_characteristics_section,
)
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME


_PROJECT_INSTRUCTIONS_MAX_BYTES = 32_768


def create_assistant_base_prompt(
    *,
    native_search_enabled: bool = False,
    fixed_persona: str | None = None,
) -> AgentMiddleware:
    """Build the stable core instructions."""

    @dynamic_prompt
    def assistant_base_prompt(
        request: ModelRequest[AssistantRunContext],
    ) -> SystemMessage:
        persona = fixed_persona or request.runtime.context.persona
        return _prepend_sections(
            request.system_message,
            [
                render_assistant_core_prompt(
                    native_search_enabled=native_search_enabled,
                    persona=persona,
                )
            ],
        )

    return assistant_base_prompt


class PersonaModelSettingsMiddleware(AgentMiddleware):
    """Apply the selected user-facing persona's output settings."""

    def wrap_model_call(self, request, handler):
        return handler(self._prepare_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare_request(request))

    @staticmethod
    def _prepare_request(request: ModelRequest) -> ModelRequest:
        if request.runtime.context.persona != "call_assistant":
            return request
        return request.override(
            model_settings={
                **request.model_settings,
                "max_tokens": MEDIA_OUTPUT_TOKEN_LIMIT,
            }
        )


def create_assistant_runtime_prompt(
    current_location: str | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentMiddleware:
    """Append user and current-message facts by volatility."""

    @dynamic_prompt
    def assistant_runtime_prompt(
        request: ModelRequest[AssistantRunContext],
    ) -> SystemMessage:
        sections = [
            render_working_directory_section(request.runtime.context.cwd),
            render_user_characteristics_section(
                current_location=current_location,
                clock=clock,
            ),
        ]
        media = render_current_media_section(
            latest_runtime_media(request.state),
            live_view_inspect_exposed=any(
                getattr(tool, "name", None) == LIVE_VIEW_INSPECT_TOOL_NAME
                for tool in request.tools
            ),
        )
        if media:
            sections.append(media)
        return _append_sections(request.system_message, sections)

    return assistant_runtime_prompt


def load_project_instructions(
    cwd: str | Path,
    *,
    host_root: str | Path | None = None,
    max_bytes: int = _PROJECT_INSTRUCTIONS_MAX_BYTES,
) -> tuple[tuple[Path, str], ...]:
    """Load bounded ASSISTANT instructions from the host root through cwd."""

    root = Path(host_root or Path.home()).resolve()
    current = Path(cwd).resolve()
    if max_bytes <= 0 or not current.is_relative_to(root):
        return ()
    directories = [root]
    for part in current.relative_to(root).parts:
        directories.append(directories[-1] / part)
    sources: list[Path] = []
    for directory in directories:
        source = directory / "ASSISTANT.md"
        if not source.is_file():
            continue
        try:
            target = source.resolve(strict=True)
        except OSError:
            continue
        if target.is_relative_to(root):
            sources.append(source)
    remaining = max_bytes
    result: list[tuple[Path, str]] = []
    for source in reversed(sources):
        try:
            raw = source.read_bytes()[:remaining]
        except OSError:
            continue
        content = raw.decode("utf-8", errors="replace")
        result.append((source, content))
        remaining -= len(raw)
        if remaining <= 0:
            break
    return tuple(reversed(result))


def render_working_directory_section(cwd: Path) -> str:
    instructions = load_project_instructions(cwd)
    rendered = "\n\n".join(f"### {path}\n\n{content}" for path, content in instructions)
    section = f"## 当前工作目录\n\n`{cwd}`"
    if rendered:
        section += (
            "\n\n## 项目指令\n\n"
            "项目指令按目录从上到下生效；发生冲突时，距离当前工作目录最近的指令优先。"
            f"\n\n{rendered}"
        )
    return section


def render_assistant_core_prompt(
    *,
    native_search_enabled: bool = False,
    persona: str = "general",
) -> str:
    """Render stable, provider-neutral operating rules."""
    renderer = (
        render_media_prompt
        if persona == "call_assistant"
        else render_studio_prompt
    )
    return renderer(native_search_enabled=native_search_enabled)


def render_current_media_section(
    media: RuntimeMediaSnapshot,
    *,
    live_view_inspect_exposed: bool = False,
) -> str:
    """Translate trusted message provenance into concise model guidance."""

    sections: list[str] = []
    if media.has_uploaded_media:
        sections.append(
            "当前用户请求包含主动上传的图片或视频。只有问题确实依赖附件内容时，"
            "才使用当前可见的 uploaded_media_inspect 获取证据。"
        )
    if live_view_inspect_exposed:
        sections.append(
            "## 视觉理解回复规则\n\n"
            "自然亲切的回答用户问题。\n"
        )
    if not sections:
        return ""
    return "\n\n".join(
        section
        if section.startswith("## ")
        else f"## 当前媒体上下文\n\n{section}"
        for section in sections
    )


def _prepend_sections(
    system_message: SystemMessage | None,
    sections: Sequence[str],
) -> SystemMessage:
    prefix = "\n\n".join(section.strip() for section in sections if section.strip())
    if system_message is None:
        return SystemMessage(content=prefix)
    return system_message.model_copy(
        update={"content": _merge_content(prefix, system_message.content, prepend=True)}
    )


def _append_sections(
    system_message: SystemMessage | None,
    sections: Sequence[str],
) -> SystemMessage:
    suffix = "\n\n".join(section.strip() for section in sections if section.strip())
    if system_message is None:
        return SystemMessage(content=suffix)
    return system_message.model_copy(
        update={
            "content": _merge_content(suffix, system_message.content, prepend=False)
        }
    )


def _merge_content(
    section: str,
    current: str | list[object],
    *,
    prepend: bool,
) -> str | list[object]:
    if isinstance(current, str):
        ordered = (section, current) if prepend else (current, section)
        return "\n\n".join(value.strip() for value in ordered if value.strip())
    if all(
        isinstance(block, dict)
        and set(block) <= {"type", "text"}
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        for block in current
    ):
        text = "".join(
            str(block["text"]) for block in current if isinstance(block, dict)
        )
        ordered = (section, text) if prepend else (text, section)
        return [
            {
                "type": "text",
                "text": "\n\n".join(
                    value.strip() for value in ordered if value.strip()
                ),
            }
        ]
    block = {
        "type": "text",
        "text": f"{section}\n\n" if prepend else f"\n\n{section}",
    }
    return [block, *current] if prepend else [*current, block]


__all__ = [
    "create_assistant_base_prompt",
    "create_assistant_runtime_prompt",
    "PersonaModelSettingsMiddleware",
    "load_project_instructions",
    "render_assistant_core_prompt",
    "render_current_media_section",
    "render_working_directory_section",
]

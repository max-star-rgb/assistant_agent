"""Project generated-image artifacts into standard assistant messages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from assistant_agent.runtime.generated_artifacts import (
    GENERATED_ARTIFACT_PUBLIC_PREFIX,
    MAX_DELIVERED_IMAGE_COUNT,
)
from assistant_agent.tools.ids import IMAGE_GENERATION_TOOL_NAME


def generated_image_output_refs(messages: Sequence[Any]) -> list[str]:
    """Return successful managed image refs produced in the latest user turn."""

    refs: list[str] = []
    for message in reversed(messages):
        if _is_human_message(message):
            break
        data = _message_data(message)
        if not _is_successful_image_tool_message(message, data):
            continue
        artifact = data.get("artifact")
        if not isinstance(artifact, Mapping):
            continue
        candidates = artifact.get("download_urls")
        if not isinstance(candidates, (list, tuple)) or not candidates:
            candidates = [artifact.get("output_ref")]
        for candidate in candidates:
            if not _is_managed_generated_ref(candidate) or candidate in refs:
                continue
            refs.append(candidate)
            if len(refs) >= MAX_DELIVERED_IMAGE_COUNT:
                return refs
    return refs


def project_generated_images(
    messages: Sequence[Any],
    *,
    artifact_base_url: str | None,
) -> AIMessage | None:
    """Attach generated images to the latest AIMessage for native chat UIs."""

    base_url = str(artifact_base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    output_refs = generated_image_output_refs(messages)
    if not output_refs:
        return None
    final_message = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    if final_message is None:
        return None

    if isinstance(final_message.content, str):
        content: list[str | dict[str, Any]] = [
            {"type": "text", "text": final_message.content}
        ]
    else:
        content = list(final_message.content)
    image_urls = [f"{base_url}{output_ref}" for output_ref in output_refs]
    markdown_images = [f"![生成图片]({url})" for url in image_urls]
    text_index = next(
        (
            index
            for index, block in enumerate(content)
            if isinstance(block, Mapping) and block.get("type") == "text"
        ),
        None,
    )
    existing_text = "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    missing_markdown = [
        markdown for markdown in markdown_images if markdown not in existing_text
    ]
    if missing_markdown:
        markdown_suffix = "\n\n".join(missing_markdown)
        if text_index is None:
            content.insert(0, {"type": "text", "text": markdown_suffix})
        else:
            text_block = dict(content[text_index])
            text = str(text_block.get("text", "")).rstrip()
            text_block["text"] = (
                f"{text}\n\n{markdown_suffix}" if text else markdown_suffix
            )
            content[text_index] = text_block
    existing_urls = {
        str(block.get("url"))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "image"
    }
    for output_ref, url in zip(output_refs, image_urls, strict=True):
        if url in existing_urls:
            continue
        path = PurePosixPath(output_ref)
        content.append(
            {
                "type": "image",
                "url": url,
                "id": path.stem,
                "mime_type": _image_mime_type(path.suffix),
            }
        )
        existing_urls.add(url)
    if content == final_message.content:
        return None
    return final_message.model_copy(update={"content": content})


def _message_data(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        nested = message.get("data")
        return nested if isinstance(nested, Mapping) else message
    return {
        "type": getattr(message, "type", None),
        "name": getattr(message, "name", None),
        "status": getattr(message, "status", None),
        "artifact": getattr(message, "artifact", None),
    }


def _is_human_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return True
    data = _message_data(message)
    return data.get("type") == "human" or data.get("role") == "user"


def _is_successful_image_tool_message(
    message: Any,
    data: Mapping[str, Any],
) -> bool:
    if not isinstance(message, ToolMessage) and data.get("type") != "tool":
        return False
    if data.get("name") != IMAGE_GENERATION_TOOL_NAME:
        return False
    if data.get("status") == "error":
        return False
    artifact = data.get("artifact")
    return isinstance(artifact, Mapping) and artifact.get("status") == "succeeded"


def _is_managed_generated_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    if not value.startswith(prefix):
        return False
    filename = value.removeprefix(prefix)
    return bool(filename) and PurePosixPath(filename).name == filename


def _image_mime_type(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix.lower(), "image/png")


__all__ = ["generated_image_output_refs", "project_generated_images"]

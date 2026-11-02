"""Resolve internal image references from successful native Tool artifacts."""

from collections.abc import Mapping

from langchain_core.messages import ToolMessage


def latest_image_ref(state: object, *, tool_names: tuple[str, ...]) -> str | None:
    if not isinstance(state, Mapping):
        return None
    for message in reversed(state.get("messages", ())):
        if (
            not isinstance(message, ToolMessage)
            or message.name not in tool_names
            or message.status == "error"
            or not isinstance(message.artifact, Mapping)
        ):
            continue
        images = message.artifact.get("images")
        if isinstance(images, (list, tuple)):
            for image in reversed(images):
                if not isinstance(image, Mapping):
                    continue
                output_ref = image.get("output_ref")
                if isinstance(output_ref, str) and output_ref.strip():
                    return output_ref.strip()
    return None

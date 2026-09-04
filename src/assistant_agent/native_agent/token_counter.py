"""Model-tokenizer-backed accounting for native Agent messages."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from langchain_core.messages import (
    MessageLikeRepresentation,
    convert_to_openai_messages,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from assistant_agent.config.models import chat_model_runtime_profile

if TYPE_CHECKING:
    from assistant_agent.config import ChatConfig
    from assistant_agent.provider_mode import ProviderMode


class ContextTokenCounter(Protocol):
    """Count tokens with the tokenizer used by the target chat model."""

    tokenizer_id: str

    def count_text(self, value: str) -> int:
        """Return the number of model tokens in one text value."""

    def count_messages(
        self,
        messages: Iterable[MessageLikeRepresentation],
        *,
        tools: list[BaseTool | dict[str, Any]] | None = None,
    ) -> int:
        """Count one LangChain message history for native middleware."""


class TokenizerJsonTokenCounter:
    """Load a local Hugging Face tokenizer.json without network access.

    The Provider applies its private chat template after receiving the request.
    We therefore tokenize a stable provider-payload projection and reserve a
    configurable safety margin at the window-policy layer. Provider-reported
    usage remains the post-call source of truth used to observe projection
    error.
    """

    def __init__(self, tokenizer_path: str | Path, *, tokenizer_id: str) -> None:
        path = Path(tokenizer_path).expanduser()
        if not path.is_file():
            raise ValueError(f"context tokenizer file does not exist: {path}")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                "tokenizer-backed context compaction requires the 'tokenizers' package"
            ) from exc
        self._tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer_id = tokenizer_id
        self._message_encoder = _load_deepseek_v4_encoder(path, tokenizer_id)

    def count_text(self, value: str) -> int:
        if not value:
            return 0
        return len(self._tokenizer.encode(value, add_special_tokens=False).ids)

    def count_messages(
        self,
        messages: Iterable[MessageLikeRepresentation],
        *,
        tools: list[BaseTool | dict[str, Any]] | None = None,
    ) -> int:
        """Count native messages and exposed tool schemas."""

        openai_messages = cast(
            "list[dict[str, Any]]",
            convert_to_openai_messages(list(messages)),
        )
        if self._message_encoder is not None:
            encoder_messages = _project_user_content_blocks_to_text(openai_messages)
            message_tokens = self.count_text(
                self._message_encoder(encoder_messages, thinking_mode="chat")
            )
        else:
            serialized = json.dumps(
                {"messages": openai_messages},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            message_tokens = self.count_text(serialized)
        if not tools:
            return message_tokens
        serialized_tools = json.dumps(
            {"tools": [convert_to_openai_tool(tool) for tool in tools]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return message_tokens + self.count_text(serialized_tools)


def create_context_token_counter(
    config: ChatConfig,
    *,
    provider_mode: ProviderMode,
) -> ContextTokenCounter | None:
    """Create the model-derived or explicitly configured Provider token counter."""

    if provider_mode != "real":
        return None
    model_id = str(config.chat_model or config.chat_provider)
    if not config.context_tokenizer_path:
        profile = chat_model_runtime_profile(model_id)
        if profile and profile.tokenizer_repository:
            return _load_pretrained_token_counter(
                profile.tokenizer_repository,
                profile.tokenizer_revision,
                tokenizer_id=model_id,
            )
        if config.context_compactor_mode == "llm" or "deepseek-v4" in model_id.lower():
            raise ValueError(
                f"{model_id} context compaction requires a known model tokenizer or "
                "MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH"
            )
        return None
    return TokenizerJsonTokenCounter(
        config.context_tokenizer_path,
        tokenizer_id=model_id,
    )


def _load_pretrained_token_counter(
    repository: str,
    revision: str,
    *,
    tokenizer_id: str,
) -> TokenizerJsonTokenCounter:
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise RuntimeError(
            "tokenizer-backed context compaction requires the 'tokenizers' package"
        ) from exc
    counter = TokenizerJsonTokenCounter.__new__(TokenizerJsonTokenCounter)
    counter._tokenizer = Tokenizer.from_pretrained(repository, revision=revision)
    counter.tokenizer_id = tokenizer_id
    counter._message_encoder = None
    return counter


def _project_user_content_blocks_to_text(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project structured user content for a text-only message encoder."""

    projected: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            projected.append(message)
            continue
        text_parts = [
            text
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance((text := block.get("text")), str)
        ]
        projected.append({**message, "content": "\n".join(text_parts)})
    return projected


def _load_deepseek_v4_encoder(
    tokenizer_path: Path,
    tokenizer_id: str,
) -> Callable[..., str] | None:
    if "deepseek-v4" not in tokenizer_id.strip().lower():
        return None
    encoding_path = tokenizer_path.parent / "encoding" / "encoding_dsv4.py"
    if not encoding_path.is_file():
        raise ValueError(
            "DeepSeek V4 context token counting requires the official "
            f"encoding/encoding_dsv4.py beside {tokenizer_path}"
        )
    spec = importlib.util.spec_from_file_location(
        "assistant_agent_deepseek_v4_encoding",
        encoding_path,
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load DeepSeek V4 message encoding: {encoding_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    encoder = getattr(module, "encode_messages", None)
    if not callable(encoder):
        raise ValueError(
            f"DeepSeek V4 encoding does not define encode_messages: {encoding_path}"
        )
    return cast("Callable[..., str]", encoder)

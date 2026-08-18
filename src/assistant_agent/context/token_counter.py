"""Model-tokenizer-backed accounting for compiled provider requests."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from langchain_core.messages import (
    MessageLikeRepresentation,
    convert_to_openai_messages,
)

from assistant_agent.runtime.chat_adapter import ChatRequest

if TYPE_CHECKING:
    from assistant_agent.config import ProviderConfig


class ContextTokenCounter(Protocol):
    """Count tokens with the tokenizer used by the target chat model."""

    tokenizer_id: str

    def count_text(self, value: str) -> int:
        """Return the number of model tokens in one text value."""

    def count_chat_request(self, request: ChatRequest) -> int:
        """Return the preflight token count for one compiled request."""

    def count_messages(
        self,
        messages: Iterable[MessageLikeRepresentation],
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

    def count_chat_request(self, request: ChatRequest) -> int:
        payload = {
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
        }
        serialized = json.dumps(
            _without_none(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return self.count_text(serialized)

    def count_messages(
        self,
        messages: Iterable[MessageLikeRepresentation],
    ) -> int:
        """Count native messages after applying the model's chat encoding."""

        openai_messages = cast(
            "list[dict[str, Any]]",
            convert_to_openai_messages(list(messages)),
        )
        if self._message_encoder is not None:
            return self.count_text(
                self._message_encoder(openai_messages, thinking_mode="chat")
            )
        serialized = json.dumps(
            {"messages": openai_messages},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return self.count_text(serialized)


def create_context_token_counter(
    config: ProviderConfig,
) -> ContextTokenCounter | None:
    """Create the configured offline tokenizer counter for Provider input."""

    if config.provider_mode != "real":
        return None
    if not config.context_tokenizer_path:
        model_id = str(config.chat_model or "").strip().lower()
        if config.context_compactor_mode == "llm" or "deepseek-v4" in model_id:
            raise ValueError(
                "DeepSeek V4/native LLM context compaction requires "
                "MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH"
            )
        return None
    return TokenizerJsonTokenCounter(
        config.context_tokenizer_path,
        tokenizer_id=str(config.chat_model or config.chat_provider),
    )


def create_visual_context_token_counter(
    config: ProviderConfig,
) -> TokenizerJsonTokenCounter | None:
    """Create the independently configured offline visual-context tokenizer."""

    if (
        config.visual_context_compactor_mode != "llm"
        or config.provider_mode != "real"
    ):
        return None
    if not config.visual_context_tokenizer_path:
        raise ValueError(
            "LLM visual context compaction requires "
            "REALTIME_VISUAL_CONTEXT_TOKENIZER_PATH"
        )
    return TokenizerJsonTokenCounter(
        config.visual_context_tokenizer_path,
        tokenizer_id=str(config.chat_model or config.chat_provider),
    )


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_none(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


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

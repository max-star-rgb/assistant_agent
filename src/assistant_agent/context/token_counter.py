"""Model-tokenizer-backed accounting for compiled provider requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

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


def create_context_token_counter(
    config: ProviderConfig,
) -> ContextTokenCounter | None:
    """Create the configured offline tokenizer counter for LLM compaction."""

    if config.context_compactor_mode != "llm" or config.provider_mode != "real":
        return None
    if not config.context_tokenizer_path:
        raise ValueError(
            "LLM context compaction requires "
            "MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH"
        )
    return TokenizerJsonTokenCounter(
        config.context_tokenizer_path,
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

"""Explicit, bounded lookup of one trace's persisted conversation turn."""

from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.runtime.assistant_run_service import ConversationStore


DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT = 1000


class TraceConversationText(BaseModel):
    """One bounded side of a persisted conversation turn."""

    text: str
    chars: int = Field(ge=0)
    truncated: bool = False


class TraceLlmInput(BaseModel):
    """One local-only provider request captured before an LLM call."""

    iteration: int = Field(ge=1)
    span_id: str | None = None
    attempt_kind: str = "primary"
    provider: str | None = None
    model: str | None = None
    request: dict[str, Any]


class TraceLlmOutput(BaseModel):
    """Local-only Provider evidence captured at the adapter/runtime boundary."""

    iteration: int = Field(ge=1)
    span_id: str
    attempt_kind: str = "primary"
    provider: str | None = None
    model: str | None = None
    normalized_result: dict[str, Any]
    provider_protocol_response: dict[str, Any] | None = None


class TraceVlmOutput(BaseModel):
    """One normalized local-only VLM result keyed by generation span."""

    span_id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    normalized_result: dict[str, Any]


class TraceVlmInput(BaseModel):
    """One sanitized local-only VLM request keyed by generation span."""

    span_id: str = Field(min_length=1)
    normalized_input: dict[str, Any]


class TraceToolObservation(BaseModel):
    """One complete assistant-facing tool observation for local diagnostics."""

    observation_index: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    observation: dict[str, Any]
    source_tool_span_id: str | None = None
    runtime_tool_call_id: str | None = None
    provider_tool_call_id: str | None = None


class TraceToolResult(BaseModel):
    """One sanitized local-only ToolResult keyed by its execution span."""

    span_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    result: dict[str, Any]


class TraceConversationView(BaseModel):
    """Current-turn content joined outside the redacted trace store."""

    schema_version: Literal["trace_conversation_view_v1"] = "trace_conversation_view_v1"
    trace_id: str
    user: TraceConversationText
    assistant: TraceConversationText
    delivered: TraceConversationText | None = None
    llm_inputs: list[TraceLlmInput] = Field(default_factory=list)
    llm_outputs: list[TraceLlmOutput] = Field(default_factory=list)
    vlm_inputs: list[TraceVlmInput] = Field(default_factory=list)
    vlm_outputs: list[TraceVlmOutput] = Field(default_factory=list)
    tool_observations: list[TraceToolObservation] = Field(default_factory=list)
    tool_results: list[TraceToolResult] = Field(default_factory=list)


@dataclass(frozen=True)
class TraceConversationRecord:
    """One current-turn debug record keyed by trace identity."""

    user_id: str
    session_id: str
    trace_id: str
    user_text: str
    assistant_text: str
    delivered_text: str | None = None
    llm_inputs: tuple[TraceLlmInput, ...] = ()
    llm_outputs: tuple[TraceLlmOutput, ...] = ()
    vlm_inputs: tuple[TraceVlmInput, ...] = ()
    vlm_outputs: tuple[TraceVlmOutput, ...] = ()
    tool_observations: tuple[TraceToolObservation, ...] = ()
    tool_results: tuple[TraceToolResult, ...] = ()


class InMemoryTraceConversationStore:
    """Process-local current-turn text lookup for explicit trace debugging.

    This store is intentionally separate from conversation history. Failed turns
    can be inspected locally without becoming future model context.
    """

    def __init__(self, *, max_records: int = 512) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records = max_records
        self._records: list[TraceConversationRecord] = []
        self._lock = Lock()

    def append(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            record = TraceConversationRecord(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                user_text=user_text,
                assistant_text=assistant_text,
                delivered_text=existing.delivered_text if existing is not None else None,
                llm_inputs=existing.llm_inputs if existing is not None else (),
                llm_outputs=existing.llm_outputs if existing is not None else (),
                vlm_inputs=existing.vlm_inputs if existing is not None else (),
                vlm_outputs=existing.vlm_outputs if existing is not None else (),
                tool_observations=(
                    existing.tool_observations if existing is not None else ()
                ),
                tool_results=existing.tool_results if existing is not None else (),
            )
            self._replace_record(record)

    def append_llm_input(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        llm_input: TraceLlmInput,
    ) -> None:
        """Upsert one compiled LLM request, preferring adapter-boundary payloads."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            existing_inputs = existing.llm_inputs if existing is not None else ()
            inputs = tuple(
                item
                for item in existing_inputs
                if not (llm_input.span_id and item.span_id == llm_input.span_id)
            ) + (llm_input,)
            record = TraceConversationRecord(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                user_text=existing.user_text if existing is not None else "",
                assistant_text=existing.assistant_text if existing is not None else "",
                delivered_text=existing.delivered_text if existing is not None else None,
                llm_inputs=inputs[-16:],
                llm_outputs=existing.llm_outputs if existing is not None else (),
                vlm_inputs=existing.vlm_inputs if existing is not None else (),
                vlm_outputs=existing.vlm_outputs if existing is not None else (),
                tool_observations=(
                    existing.tool_observations if existing is not None else ()
                ),
                tool_results=existing.tool_results if existing is not None else (),
            )
            self._replace_record(record)

    def append_llm_output(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        llm_output: TraceLlmOutput,
    ) -> None:
        """Append bounded Provider evidence before runtime branch selection."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            outputs = (*existing.llm_outputs, llm_output) if existing is not None else (llm_output,)
            record = TraceConversationRecord(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                user_text=existing.user_text if existing is not None else "",
                assistant_text=existing.assistant_text if existing is not None else "",
                delivered_text=existing.delivered_text if existing is not None else None,
                llm_inputs=existing.llm_inputs if existing is not None else (),
                llm_outputs=outputs[-16:],
                vlm_inputs=existing.vlm_inputs if existing is not None else (),
                vlm_outputs=existing.vlm_outputs if existing is not None else (),
                tool_observations=(
                    existing.tool_observations if existing is not None else ()
                ),
                tool_results=existing.tool_results if existing is not None else (),
            )
            self._replace_record(record)

    def append_vlm_input(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        vlm_input: TraceVlmInput,
    ) -> None:
        """Upsert one sanitized VLM input by generation span."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            existing_inputs = existing.vlm_inputs if existing is not None else ()
            inputs = tuple(
                item for item in existing_inputs if item.span_id != vlm_input.span_id
            ) + (vlm_input,)
            self._replace_record(
                TraceConversationRecord(
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    user_text=existing.user_text if existing is not None else "",
                    assistant_text=(
                        existing.assistant_text if existing is not None else ""
                    ),
                    delivered_text=(
                        existing.delivered_text if existing is not None else None
                    ),
                    llm_inputs=existing.llm_inputs if existing is not None else (),
                    llm_outputs=existing.llm_outputs if existing is not None else (),
                    vlm_inputs=inputs[-16:],
                    vlm_outputs=existing.vlm_outputs if existing is not None else (),
                    tool_observations=(
                        existing.tool_observations if existing is not None else ()
                    ),
                    tool_results=(
                        existing.tool_results if existing is not None else ()
                    ),
                )
            )

    def append_vlm_output(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        vlm_output: TraceVlmOutput,
    ) -> None:
        """Upsert one bounded normalized VLM result by generation span."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            existing_outputs = existing.vlm_outputs if existing is not None else ()
            outputs = tuple(
                item for item in existing_outputs if item.span_id != vlm_output.span_id
            ) + (vlm_output,)
            self._replace_record(
                TraceConversationRecord(
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    user_text=existing.user_text if existing is not None else "",
                    assistant_text=(
                        existing.assistant_text if existing is not None else ""
                    ),
                    delivered_text=(
                        existing.delivered_text if existing is not None else None
                    ),
                    llm_inputs=existing.llm_inputs if existing is not None else (),
                    llm_outputs=existing.llm_outputs if existing is not None else (),
                    vlm_inputs=existing.vlm_inputs if existing is not None else (),
                    vlm_outputs=outputs[-16:],
                    tool_observations=(
                        existing.tool_observations if existing is not None else ()
                    ),
                    tool_results=(
                        existing.tool_results if existing is not None else ()
                    ),
                )
            )

    def append_tool_observation(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        tool_observation: TraceToolObservation,
    ) -> None:
        """Append one complete model-facing tool observation to local memory."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            observations = (
                (*existing.tool_observations, tool_observation)
                if existing is not None
                else (tool_observation,)
            )
            record = TraceConversationRecord(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                user_text=existing.user_text if existing is not None else "",
                assistant_text=existing.assistant_text if existing is not None else "",
                delivered_text=existing.delivered_text if existing is not None else None,
                llm_inputs=existing.llm_inputs if existing is not None else (),
                llm_outputs=existing.llm_outputs if existing is not None else (),
                vlm_inputs=existing.vlm_inputs if existing is not None else (),
                vlm_outputs=existing.vlm_outputs if existing is not None else (),
                tool_observations=observations[-32:],
                tool_results=existing.tool_results if existing is not None else (),
            )
            self._replace_record(record)

    def append_tool_result(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        tool_result: TraceToolResult,
    ) -> None:
        """Upsert one sanitized ToolResult by execution span."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            existing_results = existing.tool_results if existing is not None else ()
            results = tuple(
                item for item in existing_results if item.span_id != tool_result.span_id
            ) + (tool_result,)
            self._replace_record(
                TraceConversationRecord(
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    user_text=existing.user_text if existing is not None else "",
                    assistant_text=(
                        existing.assistant_text if existing is not None else ""
                    ),
                    delivered_text=(
                        existing.delivered_text if existing is not None else None
                    ),
                    llm_inputs=existing.llm_inputs if existing is not None else (),
                    llm_outputs=existing.llm_outputs if existing is not None else (),
                    vlm_inputs=existing.vlm_inputs if existing is not None else (),
                    vlm_outputs=existing.vlm_outputs if existing is not None else (),
                    tool_observations=(
                        existing.tool_observations if existing is not None else ()
                    ),
                    tool_results=results[-32:],
                )
            )

    def append_delivered(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        delivered_text: str,
    ) -> None:
        """Attach the exact entry-layer text delivered for one trace."""

        with self._lock:
            existing = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            if existing is None:
                return
            self._replace_record(
                TraceConversationRecord(
                    user_id=existing.user_id,
                    session_id=existing.session_id,
                    trace_id=existing.trace_id,
                    user_text=existing.user_text,
                    assistant_text=existing.assistant_text,
                    delivered_text=delivered_text,
                    llm_inputs=existing.llm_inputs,
                    llm_outputs=existing.llm_outputs,
                    vlm_inputs=existing.vlm_inputs,
                    vlm_outputs=existing.vlm_outputs,
                    tool_observations=existing.tool_observations,
                    tool_results=existing.tool_results,
                )
            )

    def get(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        limit: int = DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT,
        include_llm_inputs: bool = False,
        include_llm_outputs: bool = False,
        include_vlm_inputs: bool = False,
        include_vlm_outputs: bool = False,
        include_tool_observations: bool = False,
        include_tool_results: bool = False,
    ) -> TraceConversationView | None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            record = self._matching_record(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            if record is not None:
                return TraceConversationView(
                    trace_id=trace_id,
                    user=_bounded_text(record.user_text, limit=limit),
                    assistant=_bounded_text(record.assistant_text, limit=limit),
                    delivered=(
                        _bounded_text(record.delivered_text, limit=limit)
                        if record.delivered_text is not None
                        else None
                    ),
                    llm_inputs=list(record.llm_inputs) if include_llm_inputs else [],
                    llm_outputs=list(record.llm_outputs) if include_llm_outputs else [],
                    vlm_inputs=(
                        list(record.vlm_inputs) if include_vlm_inputs else []
                    ),
                    vlm_outputs=(
                        list(record.vlm_outputs) if include_vlm_outputs else []
                    ),
                    tool_observations=(
                        list(record.tool_observations)
                        if include_tool_observations
                        else []
                    ),
                    tool_results=(
                        list(record.tool_results) if include_tool_results else []
                    ),
                )
        return None

    def _matching_record(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
    ) -> TraceConversationRecord | None:
        return next(
            (
                record
                for record in reversed(self._records)
                if record.user_id == user_id
                and record.session_id == session_id
                and record.trace_id == trace_id
            ),
            None,
        )

    def _replace_record(self, record: TraceConversationRecord) -> None:
        self._records = [
            existing
            for existing in self._records
            if not (
                existing.user_id == record.user_id
                and existing.session_id == record.session_id
                and existing.trace_id == record.trace_id
            )
        ]
        self._records = [*self._records, record][-self.max_records :]


_DEFAULT_TRACE_CONVERSATION_STORE = InMemoryTraceConversationStore()


def get_default_trace_conversation_store() -> InMemoryTraceConversationStore:
    """Return the process-local current-turn trace content store."""

    return _DEFAULT_TRACE_CONVERSATION_STORE


def find_trace_conversation(
    store: ConversationStore,
    *,
    user_id: str,
    session_id: str,
    trace_id: str,
    limit: int = DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT,
) -> TraceConversationView | None:
    """Return only the matching turn, bounded by Unicode character count."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    for turn in reversed(store.get(user_id, session_id)):
        if turn.trace_id != trace_id:
            continue
        return TraceConversationView(
            trace_id=trace_id,
            user=_bounded_text(turn.user_text, limit=limit),
            assistant=_bounded_text(turn.assistant_text, limit=limit),
        )
    return None


def _bounded_text(value: str, *, limit: int) -> TraceConversationText:
    chars = len(value)
    return TraceConversationText(
        text=value[:limit],
        chars=chars,
        truncated=chars > limit,
    )

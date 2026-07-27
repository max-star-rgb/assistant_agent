"""Base contracts for tools."""

from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from assistant_agent.tools.models import ToolCategory, ToolMediaRequirement, ToolResult
from assistant_agent.providers.provider_errors import sanitize_error_message


class ToolInputValidationError(ValueError):
    """Stable tool-owned rejection raised before ToolExecutor runs the tool."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolContext(BaseModel):
    """Execution context passed to tools."""

    run_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    cancel_token: Any | None = Field(default=None, exclude=True)

    def is_cancelled(self) -> bool:
        """Return whether the current run has requested cooperative cancellation."""

        checker = getattr(self.cancel_token, "is_cancelled", None)
        if callable(checker):
            return bool(checker())
        is_set = getattr(self.cancel_token, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        cancelled = getattr(self.cancel_token, "cancelled", None)
        return bool(cancelled) if isinstance(cancelled, bool) else False


class Tool(Protocol):
    """Common tool interface."""

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    category: ToolCategory
    enabled_by_default: bool
    requires_media: list[ToolMediaRequirement]

    def run(self, input: BaseModel | dict[str, Any], context: ToolContext | None = None) -> ToolResult:
        """Execute the tool and return a structured result."""


class ToolBase:
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    category: ToolCategory = "dangerous"
    enabled_by_default = True
    requires_media: list[ToolMediaRequirement] = []
    model_hidden_input_fields: tuple[str, ...] = ()
    runtime_input_bindings: tuple[Any, ...] = ()
    host_configured_exposure: bool = False

    def run(self, input: BaseModel | dict[str, Any], context: ToolContext | None = None) -> ToolResult:
        try:
            payload = self._validate_input(input)
            return self._run(payload, context or ToolContext())
        except ValidationError as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Invalid input: {exc.errors()[0]['msg']}",
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            return ToolResult(tool_name=self.name, success=False, error=sanitize_error_message(exc))

    def _validate_input(self, input: BaseModel | dict[str, Any]) -> BaseModel:
        if isinstance(input, self.input_schema):
            return input
        return self.input_schema.model_validate(input)

    def _run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError

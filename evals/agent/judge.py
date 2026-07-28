"""LLM judge boundary used by task-local graders."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
import json
import time
from typing import Any

from openai import OpenAI

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.provider_http import (
    without_unsupported_socks_proxy_env,
)
from assistant_agent.runtime.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    OpenAICompatibleChatAdapter,
)
from evals.agent.contracts import JudgeVerdict, RunEvidence


JUDGE_TIMEOUT_ENV = "AGENT_EVAL_JUDGE_TIMEOUT_SECONDS"
JUDGE_MAX_RETRIES_ENV = "AGENT_EVAL_JUDGE_MAX_RETRIES"
ProgressCallback = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class JudgeProviderSettings:
    timeout_seconds: float = 30.0
    max_retries: int = 0

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
    ) -> JudgeProviderSettings:
        return cls(
            timeout_seconds=_positive_float(
                env.get(JUDGE_TIMEOUT_ENV),
                default=30.0,
                name=JUDGE_TIMEOUT_ENV,
            ),
            max_retries=_nonnegative_int(
                env.get(JUDGE_MAX_RETRIES_ENV),
                default=0,
                name=JUDGE_MAX_RETRIES_ENV,
            ),
        )


class ProviderLLMJudge:
    """Use an explicitly configured real Chat adapter as a strict JSON judge."""

    def __init__(
        self,
        adapter: ChatAdapter,
        *,
        settings: JudgeProviderSettings | None = None,
        langfuse: Any | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.adapter = adapter
        self.settings = settings or JudgeProviderSettings()
        self.langfuse = langfuse
        self.progress = progress

    def evaluate(
        self,
        *,
        criterion_id: str,
        rubric: str,
        evidence: RunEvidence,
    ) -> JudgeVerdict:
        started_at = time.perf_counter()
        self._report(
            "agent_eval.judge.started",
            criterion_id=criterion_id,
            task_id=evidence.task_id,
        )
        observation_context = (
            self.langfuse.start_as_current_observation(
                name=f"judge.{criterion_id}",
                as_type="evaluator",
                input={
                    "criterion_id": criterion_id,
                    "task_id": evidence.task_id,
                    "run_id": evidence.run_id,
                },
                metadata={
                    "timeout_seconds": self.settings.timeout_seconds,
                    "max_retries": self.settings.max_retries,
                    "stream": False,
                },
            )
            if self.langfuse is not None
            else nullcontext(None)
        )
        with observation_context as observation:
            try:
                verdict = self._evaluate(
                    criterion_id=criterion_id,
                    rubric=rubric,
                    evidence=evidence,
                )
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                if observation is not None:
                    observation.update(
                        output={
                            "status": "infrastructure_failure",
                            "error_type": type(exc).__name__,
                        },
                        level="ERROR",
                        status_message=str(exc),
                    )
                self._report(
                    "agent_eval.judge.failed",
                    criterion_id=criterion_id,
                    task_id=evidence.task_id,
                    elapsed_ms=elapsed_ms,
                    error_type=type(exc).__name__,
                )
                raise
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if observation is not None:
                observation.update(
                    output=verdict.model_dump(mode="json"),
                )
            self._report(
                "agent_eval.judge.completed",
                criterion_id=criterion_id,
                task_id=evidence.task_id,
                elapsed_ms=elapsed_ms,
                passed=verdict.passed,
            )
            return verdict

    def _evaluate(
        self,
        *,
        criterion_id: str,
        rubric: str,
        evidence: RunEvidence,
    ) -> JudgeVerdict:
        result = self.adapter.chat(
            ChatRequest(
                user_id="agent-eval-judge",
                session_id=f"agent-eval-judge-{evidence.task_id}",
                user_query=json.dumps(
                    {
                        "criterion_id": criterion_id,
                        "rubric": rubric,
                        "evidence": evidence.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                ),
                system_instruction=(
                    "你是严格的 Agent 评测裁判。只依据 rubric 和 evidence 判定，"
                    "不得补充缺失事实。返回符合 JSON Schema 的 passed 和 reason。"
                ),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "agent_eval_judge_verdict",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "passed": {"type": "boolean"},
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                            },
                            "required": ["passed", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                temperature=0.0,
                max_tokens=300,
            )
        )
        if result.errors:
            raise RuntimeError(
                "LLM judge Provider failed: "
                + ", ".join(error.code for error in result.errors)
            )
        try:
            payload = json.loads(result.response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM judge did not return valid JSON.") from exc
        return JudgeVerdict.model_validate(payload)

    def _report(self, event: str, **details: object) -> None:
        if self.progress is not None:
            self.progress({"event": event, **details})


def create_provider_judge(
    config: ProviderConfig,
    *,
    env: Mapping[str, str],
    langfuse: Any | None = None,
    progress: ProgressCallback | None = None,
) -> ProviderLLMJudge:
    settings = JudgeProviderSettings.from_env(env)
    provider = config.resolved_chat_provider()
    if provider.adapter_kind != "openai_compatible":
        raise RuntimeError(
            "Agent eval Judge requires an OpenAI-compatible real Chat Provider."
        )
    with without_unsupported_socks_proxy_env():
        client = OpenAI(
            api_key=provider.api_key or "",
            base_url=provider.base_url or "",
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
    adapter = OpenAICompatibleChatAdapter(
        provider=provider.provider,
        api_key=provider.api_key or "",
        base_url=provider.base_url or "",
        model=provider.model or "",
        timeout_seconds=settings.timeout_seconds,
        stream=False,
        enable_thinking=False if provider.provider == "qwen" else None,
        client=client,
    )
    return ProviderLLMJudge(
        adapter,
        settings=settings,
        langfuse=langfuse,
        progress=progress,
    )


def _positive_float(
    value: str | None,
    *,
    default: float,
    name: str,
) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number.") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive number.")
    return parsed


def _nonnegative_int(
    value: str | None,
    *,
    default: int,
    name: str,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative integer.") from exc
    if parsed < 0:
        raise RuntimeError(f"{name} must be a non-negative integer.")
    return parsed

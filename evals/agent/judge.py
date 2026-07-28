"""LLM judge boundary used by task-local graders."""

from __future__ import annotations

import json

from assistant_agent.runtime.chat_adapter import ChatAdapter, ChatRequest
from evals.agent.contracts import JudgeVerdict, RunEvidence


class ProviderLLMJudge:
    """Use an explicitly configured real Chat adapter as a strict JSON judge."""

    def __init__(self, adapter: ChatAdapter) -> None:
        self.adapter = adapter

    def evaluate(
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

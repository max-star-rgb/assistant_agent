from assistant_agent.memory.retrieval_eval import (
    evaluate_memory_retrieval_case,
    summarize_memory_retrieval_eval,
)
from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_memory_retrieval_eval_reports_recall_and_token_budget() -> None:
    result = evaluate_memory_retrieval_case(
        {
            "id": "budget_case",
            "query": "浅色",
            "max_context_chars": 1000,
            "max_context_tokens": 28,
            "expected_memory_ids": ["m1"],
            "expected_injected_ids": ["m1"],
            "forbidden_injected_ids": ["m2"],
            "fixtures": [
                {
                    "memory_id": "m1",
                    "memory_type": "preference",
                    "summary": "用户喜欢浅色。",
                    "created_at": "2026-01-02T00:00:00+00:00",
                },
                {
                    "memory_id": "m2",
                    "memory_type": "preference",
                    "summary": "用户喜欢非常详细的日系极简浅色背景。" * 8,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            ],
        }
    )

    assert result.passed is True
    assert result.recall_at_k == 1.0
    assert result.reciprocal_rank == 1.0
    assert result.injected_memory_ids == ["m1"]
    assert result.memory_tokens <= result.memory_token_budget
    assert result.omitted_count == 1
    assert result.rejected_reasons == ["m2:memory_context_token_budget_exceeded"]


def test_memory_retrieval_eval_summary_tracks_empty_and_safety_rates() -> None:
    hit = evaluate_memory_retrieval_case(
        {
            "id": "hit",
            "query": "黑色包",
            "expected_memory_ids": ["m1"],
            "fixtures": [
                {
                    "memory_id": "m1",
                    "memory_type": "product",
                    "summary": "用户上次关注了一个黑色包。",
                }
            ],
        }
    )
    empty = evaluate_memory_retrieval_case(
        {
            "id": "empty",
            "query": "玉桂狗杯子",
            "expected_empty": True,
            "fixtures": [
                {
                    "memory_id": "m1",
                    "memory_type": "product",
                    "summary": "用户上次关注了一个黑色包。",
                }
            ],
        }
    )

    summary = summarize_memory_retrieval_eval([hit, empty])

    assert summary["total"] == 2
    assert summary["passed"] == 2
    assert summary["recall_at_k"] == 1.0
    assert summary["mrr"] == 1.0
    assert summary["false_positive_rate"] == 0.0
    assert summary["correct_empty_rate"] == 1.0
    assert summary["sensitive_injection_rate"] == 0.0
    assert summary["token_budget_compliance"] == 1.0


def test_memory_retrieval_eval_excludes_superseded_profile_sources() -> None:
    result = evaluate_memory_retrieval_case(
        {
            "id": "superseded_preference",
            "query": "风格",
            "expected_memory_ids": ["style_new"],
            "expected_injected_ids": ["style_new"],
            "expected_profile_source_memory_ids": ["style_new"],
            "expected_profile_conflict_count": 1,
            "forbidden_memory_ids": ["style_old"],
            "forbidden_injected_ids": ["style_old"],
            "forbidden_profile_source_memory_ids": ["style_old"],
            "explicit_saves": [
                {
                    "memory_id": "style_old",
                    "text": "记住我喜欢浅色日系风格",
                    "content": {
                        "preference_key": "style",
                        "style": "浅色日系",
                        "summary": "用户喜欢浅色日系风格。",
                    },
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "memory_id": "style_new",
                    "text": "记住我现在喜欢深色极简风格",
                    "content": {
                        "preference_key": "style",
                        "style": "深色极简",
                        "summary": "用户喜欢深色极简风格。",
                    },
                    "created_at": "2026-01-02T00:00:00+00:00",
                },
            ],
        }
    )

    assert result.passed is True
    assert "style_new" in result.retrieved_memory_ids
    assert "style_new" in result.injected_memory_ids
    assert "style_old" not in result.retrieved_memory_ids
    assert "style_old" not in result.injected_memory_ids
    assert result.profile_source_memory_ids == ["style_new"]
    assert result.profile_conflicts[0]["superseded_memory_ids"] == ["style_old"]
    assert result.forbidden_retrieved_ids == []
    assert result.forbidden_profile_source_ids == []


def test_run_evals_memory_suite_includes_retrieval_metrics() -> None:
    cases = filter_cases_by_suite(load_cases(), "memory")

    summary = run_evals(cases, router_mode="rule")

    assert summary["failed"] == 0
    retrieval_summary = summary["memory_retrieval_eval"]
    assert retrieval_summary["total"] >= 10
    assert retrieval_summary["recall_at_k"] == 1.0
    assert retrieval_summary["correct_empty_rate"] == 1.0
    assert retrieval_summary["cross_user_leakage_rate"] == 0.0
    assert retrieval_summary["sensitive_injection_rate"] == 0.0
    assert retrieval_summary["expired_injection_rate"] == 0.0
    assert retrieval_summary["token_budget_compliance"] == 1.0

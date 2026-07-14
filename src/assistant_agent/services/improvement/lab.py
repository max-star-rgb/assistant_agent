"""Offline orchestration for evidence-backed improvement proposals."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

from assistant_agent.runtime_profile import RuntimeProfile
from assistant_agent.schemas.improvement import (
    AllowlistedEvalResult,
    CandidateCheck,
    CandidateEvaluationRecord,
    ImprovementEvidence,
    ImprovementRunIssue,
    ImprovementRunReport,
    ImprovementTargetType,
)
from assistant_agent.services.chat_adapter import ChatAdapter
from assistant_agent.services.improvement.detector import detect_opportunities
from assistant_agent.services.improvement.evaluator import evaluate_candidate
from assistant_agent.services.improvement.evaluator import run_allowlisted_test_suites
from assistant_agent.services.improvement.evidence import (
    EvidenceLoadError,
    collect_trajectory_evidence,
    deduplicate_evidence,
    load_structured_evidence,
)
from assistant_agent.services.improvement.proposer import generate_proposal
from assistant_agent.services.improvement.registry import JsonlImprovementRegistry
from assistant_agent.services.trace_store import TraceStore
from assistant_agent.services.trajectory_debug import build_redacted_trajectory_replay


def run_improvement_lab(
    *,
    trace_store: TraceStore | None,
    run_ids: Sequence[str],
    trace_ids: Sequence[str],
    eval_paths: Sequence[Path],
    test_paths: Sequence[Path],
    target_type: ImprovementTargetType | None,
    skill_id: str | None,
    repo_root: Path,
    registry_root: Path,
    persist: bool,
    proposal_mode: str,
    adapter: ChatAdapter | None = None,
    runtime_profile: RuntimeProfile | None = None,
    run_allowlisted_evals: bool = False,
    eval_runner=None,
) -> ImprovementRunReport:
    """Run one explicit analysis without changing any improvement target."""

    started_at = datetime.now(timezone.utc)
    report = ImprovementRunReport(
        run_id=f"improvement_{uuid4().hex}",
        started_at=started_at,
        analysis_cutoff=started_at - timedelta(days=30),
    )
    evidence: list[ImprovementEvidence] = []
    issues: list[ImprovementRunIssue] = []
    has_selector = bool(run_ids or trace_ids or eval_paths or test_paths)
    if not has_selector:
        issues.append(
            ImprovementRunIssue(
                code="no_evidence_sources",
                summary="No explicit trace, eval, or test evidence source was selected.",
            )
        )

    for kind, identifiers in (("run", run_ids), ("trace", trace_ids)):
        for identifier in identifiers:
            if trace_store is None:
                issues.append(
                    ImprovementRunIssue(
                        code="evidence_source_not_found",
                        source_ref=f"{kind}:{identifier}",
                        summary="No trace store was configured for the selected identifier.",
                    )
                )
                continue
            try:
                events = (
                    trace_store.list_by_run(identifier)
                    if kind == "run"
                    else trace_store.list_by_trace(identifier)
                )
            except Exception:
                issues.append(
                    ImprovementRunIssue(
                        code="evidence_source_read_failed",
                        source_ref=f"{kind}:{identifier}",
                        summary="Selected trace evidence source could not be read.",
                    )
                )
                continue
            if not events:
                issues.append(
                    ImprovementRunIssue(
                        code="evidence_source_not_found",
                        source_ref=f"{kind}:{identifier}",
                        summary="Selected trace evidence was not found.",
                    )
                )
                continue
            try:
                evidence.extend(
                    collect_trajectory_evidence(build_redacted_trajectory_replay(events))
                )
            except EvidenceLoadError as exc:
                issues.append(
                    ImprovementRunIssue(
                        code="evidence_schema_invalid",
                        source_ref=f"{kind}:{identifier}",
                        summary=str(exc),
                    )
                )

    for source_type, paths in (("eval_failure", eval_paths), ("test_failure", test_paths)):
        for path in paths:
            try:
                evidence.extend(load_structured_evidence(path, source_type=source_type))
            except EvidenceLoadError as exc:
                issues.append(
                    ImprovementRunIssue(
                        code=(
                            "evidence_source_not_found"
                            if not Path(path).exists()
                            else "evidence_schema_invalid"
                        ),
                        source_ref=str(path),
                        summary=str(exc),
                    )
                )

    evidence = deduplicate_evidence(evidence)
    opportunities = detect_opportunities(
        evidence,
        target_type=target_type,
        now=report.started_at,
        max_age_days=report.analysis_max_age_days,
    )
    if skill_id is not None:
        opportunities = [
            item
            for item in opportunities
            if item.target_type == "skill" and item.target_ref == skill_id
        ]

    candidates = []
    for opportunity in opportunities:
        if opportunity.status != "ready_for_proposal":
            continue
        proposal = generate_proposal(
            opportunity,
            evidence,
            repo_root=Path(repo_root),
            mode=proposal_mode,
            adapter=adapter,
            runtime_profile=runtime_profile,
        )
        if proposal.candidate is None:
            issues.append(
                ImprovementRunIssue(
                    code=proposal.error_code or "proposal_failed",
                    source_ref=opportunity.opportunity_id,
                    summary=proposal.error_summary or "Proposal generation failed.",
                )
            )
            continue
        candidate = evaluate_candidate(
            proposal,
            opportunity,
            evidence,
            repo_root=Path(repo_root),
        )
        if candidate is not None:
            candidates.append(candidate)

    validation_results = []
    if run_allowlisted_evals:
        kwargs = {"repo_root": Path(repo_root), "run_id": report.run_id}
        if eval_runner is not None:
            kwargs["runner"] = eval_runner
        validation_results = run_allowlisted_test_suites(candidates, **kwargs)
        candidates = _apply_validation_results(candidates, validation_results)

    persisted = False
    if persist:
        registry = JsonlImprovementRegistry(registry_root)
        evaluation_records = [
            CandidateEvaluationRecord(
                evaluation_id=_evaluation_id(report.run_id, candidate.candidate_id),
                run_id=report.run_id,
                candidate_id=candidate.candidate_id,
                evaluation=candidate.evaluation,
            )
            for candidate in candidates
        ]
        registry.append_evidence(evidence)
        registry.append_opportunities(opportunities)
        registry.append_candidates(candidates)
        registry.append_candidate_evaluations(evaluation_records)
        registry.append_validation_results(validation_results)
        issues.extend(
            ImprovementRunIssue(code=code, summary="Registry skipped an invalid existing record.")
            for code in registry.issues
        )
        persisted = True

    return report.model_copy(
        update={
            "completed_at": datetime.now(timezone.utc),
            "evidence": evidence,
            "opportunities": opportunities,
            "candidates": candidates,
            "issues": issues,
            "validation_results": validation_results,
            "persisted": persisted,
            "production_mutation_allowed": False,
        }
    )


def _apply_validation_results(
    candidates,
    results: list[AllowlistedEvalResult],
):
    """Attach fixed-suite outcomes before candidates are persisted or reported."""

    by_suite = {result.suite_id: result for result in results}
    updated = []
    for candidate in candidates:
        relevant = [
            by_suite[suite_id]
            for suite_id in candidate.suggested_test_suite_ids
            if suite_id in by_suite
        ]
        if not relevant:
            updated.append(candidate)
            continue
        checks = list(candidate.evaluation.checks)
        blocked = list(candidate.evaluation.blocked_reasons)
        failed = False
        for result in relevant:
            passed = result.status == "passed"
            failed = failed or not passed
            checks.append(
                CandidateCheck(
                    check_name=f"allowlisted_eval:{result.suite_id}",
                    status="passed" if passed else "failed",
                    summary=result.summary,
                )
            )
        if failed and "allowlisted_eval_failed" not in blocked:
            blocked.append("allowlisted_eval_failed")
        evaluation = candidate.evaluation.model_copy(
            update={
                "checks": checks,
                "blocked_reasons": blocked,
                "score": None if failed else candidate.evaluation.score,
                "ready_for_review": candidate.evaluation.ready_for_review and not failed,
            }
        )
        updated.append(
            candidate.model_copy(
                update={
                    "evaluation": evaluation,
                    "status": "evaluation_failed" if failed else candidate.status,
                }
            )
        )
    return updated


def _evaluation_id(run_id: str, candidate_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{candidate_id}".encode("utf-8")).hexdigest()[:20]
    return f"evaluation_{digest}"

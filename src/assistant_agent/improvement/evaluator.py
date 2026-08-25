"""Independent local gates for generated improvement candidates."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable

from deepagents.middleware.skills import SkillMetadata

from assistant_agent.improvement.models import (
    AllowlistedEvalResult,
    CandidateCheck,
    CandidateEvaluation,
    ImprovementCandidate,
    ImprovementEvidence,
    ImprovementOpportunity,
)
from assistant_agent.skills.native import (
    create_project_skills_backend,
    list_skill_reference_ids,
    load_project_skills_metadata,
)
from assistant_agent.improvement.proposer import ProposalResult
from assistant_agent.improvement.paths import ImprovementTargetPathError, resolve_repo_skill_file
from assistant_agent.improvement.evidence import validate_prompt_safe_payload


_MINIMAL_PYTEST_COMMAND = ("python", "-m", "pytest", "-q")

TEST_SUITE_COMMANDS: dict[str, tuple[str, ...]] = {
    name: _MINIMAL_PYTEST_COMMAND
    for name in (
        "skill_manifest",
        "skill_tool_contract",
        "tool_catalog",
        "assistant_loop",
        "context_budget",
        "provider_adapter",
        "targeted_pytest",
    )
}

_BYPASS_MARKERS = (
    "bypass actionvalidator",
    "bypass action validator",
    "toolregistry.run directly",
    "disable redaction",
    "enable real provider by default",
)
_BYPASS_PATTERN = re.compile(
    r"\b(bypass|remove|disable|skip|avoid|turn off)\b.{0,60}\b"
    r"(actionvalidator|toolexecutor|toolregistry|validator|executor|registry|audit|redaction|policy|identity)\b|"
    r"\b(call|invoke|use)\b.{0,40}\bregistry\b.{0,20}\bdirect",
    re.IGNORECASE,
)
_DIRECT_EXECUTION_MARKERS = (
    "run_skill",
    "curl ",
    "http://",
    "https://",
    "subprocess",
    "os.system",
    "toolregistry.run",
    "direct provider",
    "raw shell",
    "wget ",
    "execute bash",
    "provider api directly",
)
_DIRECT_EXECUTION_PATTERN = re.compile(
    r"\b(execute|run|use|invoke|call|launch|start)\b.{0,40}\b"
    r"(bash|zsh|powershell|cmd|shell|wget|curl|browser|provider\s+api)\b|"
    r"\b(call|contact|invoke|use)\b.{0,40}\b"
    r"(openai|anthropic|gemini|provider(?:\s+api)?)\b.{0,40}\bdirect(?:ly)?\b|"
    r"\b(enable|activate|select)\b.{0,30}\b(real|live|external)\s+provider\b"
    r".{0,30}\b(automatic(?:ally)?|by\s+default)\b|"
    r"\b(ignore|bypass|remove|disable|skip|avoid|turn off)\b.{0,60}\b"
    r"(audit|validator|executor|registry|redaction|policy|identity|memory)\b",
    re.IGNORECASE,
)


def evaluate_candidate(
    result: ProposalResult,
    opportunity: ImprovementOpportunity,
    evidence: list[ImprovementEvidence],
    *,
    repo_root: Path,
) -> ImprovementCandidate | None:
    """Apply local hard gates and return an evaluated, still-non-mutating candidate."""

    candidate = result.candidate
    if candidate is None:
        return None
    checks: list[CandidateCheck] = []
    blocked: list[str] = []

    _check(
        checks,
        blocked,
        "schema_valid",
        True,
        "Candidate schema is valid.",
        "candidate_schema_invalid",
    )
    evidence_sufficient = opportunity.status == "ready_for_proposal"
    _check(
        checks,
        blocked,
        "evidence_sufficient",
        evidence_sufficient,
        "Opportunity passed deterministic evidence eligibility.",
        "evidence_insufficient",
        opportunity.evidence_refs,
    )
    available_ids = {item.evidence_id for item in evidence}
    citations_valid = (
        set(candidate.evidence_refs) == set(opportunity.evidence_refs)
        and set(candidate.evidence_refs).issubset(available_ids)
    )
    _check(
        checks,
        blocked,
        "evidence_citations_valid",
        citations_valid,
        "Candidate cites exactly the opportunity evidence.",
        "evidence_citations_invalid",
        candidate.evidence_refs,
    )
    target_valid = (
        candidate.opportunity_id == opportunity.opportunity_id
        and candidate.target_type == opportunity.target_type
        and candidate.target_ref == opportunity.target_ref
    )
    _check(
        checks,
        blocked,
        "target_scope_valid",
        target_valid,
        "Candidate target matches the opportunity.",
        "candidate_target_mismatch",
    )
    combined = " ".join(
        [candidate.root_cause_hypothesis, candidate.proposed_change, *candidate.affected_locations]
    ).lower()
    boundary_valid = not any(marker in combined for marker in _BYPASS_MARKERS) and not _BYPASS_PATTERN.search(combined)
    _check(
        checks,
        blocked,
        "architecture_boundary_passed",
        boundary_valid,
        "Proposal preserves assistant governance boundaries.",
        "architecture_boundary_failed",
    )
    acceptance_valid = all(_is_measurable_criterion(item) for item in candidate.acceptance_criteria)
    _check(
        checks,
        blocked,
        "acceptance_criteria_measurable",
        acceptance_valid,
        "Acceptance criteria are concrete enough for review.",
        "acceptance_criteria_not_measurable",
    )
    suites_valid = bool(candidate.suggested_test_suite_ids) and all(
        suite_id in TEST_SUITE_COMMANDS for suite_id in candidate.suggested_test_suite_ids
    )
    _check(
        checks,
        blocked,
        "suggested_tests_allowlisted",
        suites_valid,
        "Suggested test suites are repository-owned.",
        "suggested_tests_not_allowlisted",
    )

    patch_preview = candidate.patch_preview
    if candidate.target_type == "skill":
        patch_preview = _evaluate_skill_replacement(
            result,
            candidate,
            Path(repo_root),
            checks,
            blocked,
        )
    else:
        _check(
            checks,
            blocked,
            "patch_scope_valid",
            candidate.patch_preview is None and result.replacement_skill_content is None,
            "Runtime and code recommendations contain no patch payload.",
            "non_skill_patch_forbidden",
        )

    ready = not blocked
    limitations = list(candidate.limitations)
    semantic_limitation = "Semantic scope was not evaluated automatically."
    if semantic_limitation not in limitations:
        limitations.append(semantic_limitation)
    checks.append(
        CandidateCheck(
            check_name="semantic_scope_preserved",
            status="not_run",
            summary="No target-specific semantic eval was run in this evaluation.",
        )
    )
    evaluation = CandidateEvaluation(
        checks=checks,
        regression_suites=candidate.suggested_test_suite_ids,
        blocked_reasons=blocked,
        score=0.7 if ready else None,
        ready_for_review=ready,
    )
    return candidate.model_copy(
        update={
            "patch_preview": patch_preview,
            "evaluation": evaluation,
            "limitations": limitations,
            "status": "ready_for_review" if ready else "evaluation_failed",
        }
    )


def resolved_test_commands(candidate: ImprovementCandidate) -> list[str]:
    """Resolve symbolic suites to display-only repository commands."""

    return [
        " ".join(TEST_SUITE_COMMANDS[suite_id])
        for suite_id in candidate.suggested_test_suite_ids
        if suite_id in TEST_SUITE_COMMANDS
    ]


def run_allowlisted_test_suites(
    candidates: list[ImprovementCandidate],
    *,
    repo_root: Path,
    runner: Callable[..., Any] = subprocess.run,
    run_id: str = "validation_run",
) -> list[AllowlistedEvalResult]:
    """Run fixed local test commands selected by review-ready candidates."""

    suite_ids = sorted(
        {
            suite_id
            for candidate in candidates
            if candidate.status == "ready_for_review"
            for suite_id in candidate.suggested_test_suite_ids
            if suite_id in TEST_SUITE_COMMANDS
        }
    )
    results: list[AllowlistedEvalResult] = []
    allowed_environment_keys = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH")
    offline_environment = {
        key: os.environ[key] for key in allowed_environment_keys if key in os.environ
    }
    offline_environment.update(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "PYTHONHASHSEED": "0",
        }
    )
    for suite_id in suite_ids:
        configured = list(TEST_SUITE_COMMANDS[suite_id])
        command = [sys.executable, *configured[1:]]
        try:
            completed = runner(
                command,
                cwd=Path(repo_root),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                shell=False,
                env=offline_environment,
            )
            passed = completed.returncode == 0
            validation_id = _validation_id(run_id, suite_id, command)
            results.append(
                AllowlistedEvalResult(
                    validation_id=validation_id,
                    run_id=run_id,
                    suite_id=suite_id,
                    command=" ".join(command),
                    status="passed" if passed else "failed",
                    returncode=completed.returncode,
                    summary=(
                        "Allowlisted local suite passed."
                        if passed
                        else "Allowlisted local suite failed; inspect the local test output."
                    ),
                )
            )
        except Exception:
            results.append(
                AllowlistedEvalResult(
                    validation_id=_validation_id(run_id, suite_id, command),
                    run_id=run_id,
                    suite_id=suite_id,
                    command=" ".join(command),
                    status="error",
                    summary="Allowlisted local suite could not be executed.",
                )
            )
    return results


def _evaluate_skill_replacement(
    result: ProposalResult,
    candidate: ImprovementCandidate,
    repo_root: Path,
    checks: list[CandidateCheck],
    blocked: list[str],
) -> str | None:
    relative_path = Path("skills") / candidate.target_ref / "SKILL.md"
    try:
        current_path = resolve_repo_skill_file(repo_root, candidate.target_ref)
        current_content = current_path.read_text(encoding="utf-8")
    except (OSError, ImprovementTargetPathError):
        _check(
            checks,
            blocked,
            "skill_target_available",
            False,
            "Current skill target could not be read.",
            "skill_target_unavailable",
        )
        return None
    replacement = result.replacement_skill_content
    _check(
        checks,
        blocked,
        "skill_replacement_present",
        bool(replacement),
        "Replacement skill content is present.",
        "skill_replacement_missing",
    )
    if not replacement:
        return None

    replacement_safe = not validate_prompt_safe_payload(replacement)
    _check(
        checks,
        blocked,
        "skill_replacement_prompt_safe",
        replacement_safe,
        "Replacement contains no unsafe raw or secret-like payload.",
        "skill_replacement_unsafe",
    )

    current_descriptor = _load_descriptor(repo_root, candidate.target_ref)
    replacement_descriptor = _load_replacement_descriptor(
        repo_root,
        candidate.target_ref,
        replacement,
    )
    manifest_valid = current_descriptor is not None and replacement_descriptor is not None
    _check(
        checks,
        blocked,
        "skill_manifest_passed",
        manifest_valid,
        "Replacement passes the native Agent Skills loader.",
        "skill_manifest_invalid",
    )
    if not manifest_valid:
        return None
    assert current_descriptor is not None
    assert replacement_descriptor is not None
    current_metadata, current_references = current_descriptor
    replacement_metadata, replacement_references = replacement_descriptor
    tools_preserved = set(replacement_metadata["allowed_tools"]) == set(
        current_metadata["allowed_tools"]
    )
    _check(
        checks,
        blocked,
        "skill_governed_tools_preserved",
        tools_preserved,
        "Governed tools are unchanged.",
        "skill_governed_tool_expansion",
    )
    activation_preserved = replacement_metadata["name"] == current_metadata["name"]
    _check(
        checks,
        blocked,
        "skill_activation_contract_preserved",
        activation_preserved,
        "Native Skill identity is unchanged.",
        "skill_activation_contract_changed",
    )
    references_preserved = replacement_references == current_references
    _check(
        checks,
        blocked,
        "skill_references_preserved",
        references_preserved,
        "Automatically discovered Skill references are unchanged.",
        "skill_references_changed",
    )
    discovery_preserved = replacement_metadata["path"].endswith(
        f"/{replacement_metadata['name']}/SKILL.md"
    )
    _check(
        checks,
        blocked,
        "skill_discovery_contract_preserved",
        discovery_preserved,
        "Native Skill discovery path remains standard.",
        "skill_discovery_contract_changed",
    )
    purpose_preserved = _description_overlap(
        current_metadata["description"],
        replacement_metadata["description"],
    ) >= 0.25
    _check(
        checks,
        blocked,
        "skill_purpose_preserved",
        purpose_preserved,
        "Replacement description preserves the original capability purpose.",
        "skill_purpose_drift",
    )
    replacement_lower = replacement.lower()
    direct_execution_safe = (
        not any(marker in replacement_lower for marker in _DIRECT_EXECUTION_MARKERS)
        and not _DIRECT_EXECUTION_PATTERN.search(replacement)
        and not _BYPASS_PATTERN.search(replacement)
    )
    _check(
        checks,
        blocked,
        "skill_direct_execution_absent",
        direct_execution_safe,
        "Replacement contains no direct execution instructions.",
        "skill_direct_execution_instruction",
    )
    patch = "".join(
        difflib.unified_diff(
            current_content.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile=f"a/{relative_path.as_posix()}",
            tofile=f"b/{relative_path.as_posix()}",
        )
    )
    _check(
        checks,
        blocked,
        "patch_parse_passed",
        bool(patch),
        "A deterministic single-file diff preview was generated.",
        "skill_replacement_has_no_change",
    )
    return patch or None


def _load_descriptor(
    repo_root: Path,
    skill_id: str,
) -> tuple[SkillMetadata, tuple[str, ...]] | None:
    backend = create_project_skills_backend(repo_root / "skills")
    metadata = next(
        (
            item
            for item in load_project_skills_metadata(backend)
            if item["name"] == skill_id
        ),
        None,
    )
    if metadata is None:
        return None
    return metadata, tuple(list_skill_reference_ids(backend, metadata))


def _load_replacement_descriptor(
    repo_root: Path,
    skill_id: str,
    content: str,
) -> tuple[SkillMetadata, tuple[str, ...]] | None:
    with TemporaryDirectory(prefix="assistant-agent-skill-eval-") as directory:
        root = Path(directory)
        source_dir = repo_root / "skills" / skill_id
        target_dir = root / "skills" / skill_id
        try:
            shutil.copytree(source_dir, target_dir)
        except OSError:
            return None
        skill_path = target_dir / "SKILL.md"
        skill_path.write_text(content, encoding="utf-8")
        return _load_descriptor(root, skill_id)


def _is_measurable_criterion(value: str) -> bool:
    lowered = value.strip().lower()
    if len(lowered) < 12:
        return False
    markers = ("pass", "fail", "terminat", "remain", "returns", "equals", "count", "latency", "rate", "no ", "<=", ">=", "%")
    return any(marker in lowered for marker in markers)


def _description_overlap(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9_]+", left.lower()))
    right_tokens = set(re.findall(r"[a-z0-9_]+", right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _check(
    checks: list[CandidateCheck],
    blocked: list[str],
    name: str,
    passed: bool,
    summary: str,
    blocked_reason: str,
    evidence_refs: list[str] | None = None,
) -> None:
    checks.append(
        CandidateCheck(
            check_name=name,
            status="passed" if passed else "failed",
            summary=summary,
            evidence_refs=evidence_refs or [],
        )
    )
    if not passed:
        blocked.append(blocked_reason)


def _validation_id(run_id: str, suite_id: str, command: list[str]) -> str:
    payload = json.dumps(
        {"run_id": run_id, "suite_id": suite_id, "command": command},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"validation_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"

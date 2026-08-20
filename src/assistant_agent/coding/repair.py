"""Pure contracts and policy for bounded coding validation repair."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import ValidationError

from assistant_agent.coding.models import (
    CodingRepairApprovalContext,
    CodingRepairApprovalDecision,
    CodingRepairFailureEvidence,
    CodingVerificationResult,
)


MAX_REPAIR_ROUNDS = 2


def select_repairable_failure(
    result: CodingVerificationResult,
    repair_round: int,
) -> CodingRepairFailureEvidence | None:
    """Return the single normal command failure eligible for repair."""
    if result.status != "failed" or repair_round >= MAX_REPAIR_ROUNDS:
        return None
    eligible = [
        evidence
        for evidence in result.evidence
        if evidence.kind in {"test", "lint", "build"}
        and evidence.status == "failed"
        and evidence.exit_code is not None
        and evidence.exit_code != 0
        and evidence.error_code == "verification_command_failed"
    ]
    if len(eligible) != 1:
        return None
    return CodingRepairFailureEvidence.model_validate(
        eligible[0].model_dump(
            include={
                "command_id",
                "kind",
                "exit_code",
                "error_code",
                "output_digest",
                "stdout",
                "stderr",
                "truncated",
            }
        )
    )


def render_repair_context(
    evidence: CodingRepairFailureEvidence,
    repair_round: int,
) -> str:
    """Render the fixed, bounded repair prompt without host metadata."""
    remaining_rounds = MAX_REPAIR_ROUNDS - repair_round
    return (
        f"Validation failed during repair round {repair_round}; "
        f"{remaining_rounds} remaining.\n"
        "Inspect the current coding workspace with the coding read Tool and submit "
        "one minimal incremental patch.\n"
        f"command_id: {evidence.command_id}\n"
        f"kind: {evidence.kind}\n"
        f"exit_code: {evidence.exit_code}\n"
        f"error_code: {evidence.error_code}\n"
        f"output_digest: {evidence.output_digest}\n"
        f"truncated: {evidence.truncated}\n"
        f"stdout:\n{evidence.stdout}\n"
        f"stderr:\n{evidence.stderr}"
    )


def repair_interrupt_payload(
    context: CodingRepairApprovalContext,
) -> dict[str, object]:
    """Return the digest-bound interrupt payload for a repair patch approval."""
    return {
        "action": "coding_patch_apply",
        "origin": "repair",
        "repair_round": context.repair_round,
        "patch_digest": context.patch_digest,
        "workspace_diff_digest": context.workspace_diff_digest,
        "candidate_diff_digest": context.candidate_diff_digest,
        "cumulative_diff_preview": context.cumulative_diff_preview,
    }


def validate_repair_approval(
    context: CodingRepairApprovalContext,
    raw: object,
) -> Literal["approve", "reject", "respond"]:
    """Validate a repair approval against every digest in its context."""
    if not isinstance(raw, Mapping):
        raise ValueError("coding_approval_mismatch")
    try:
        decision = CodingRepairApprovalDecision.model_validate(dict(raw))
    except ValidationError as exc:
        raise ValueError("coding_approval_mismatch") from exc
    if decision.decision == "approve" and (
        decision.patch_digest != context.patch_digest
        or decision.workspace_diff_digest != context.workspace_diff_digest
        or decision.candidate_diff_digest != context.candidate_diff_digest
    ):
        raise ValueError("coding_approval_mismatch")
    return decision.decision


__all__ = [
    "MAX_REPAIR_ROUNDS",
    "repair_interrupt_payload",
    "render_repair_context",
    "select_repairable_failure",
    "validate_repair_approval",
]

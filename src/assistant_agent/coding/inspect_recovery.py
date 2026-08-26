"""Canonical, checkpoint-safe primary coding inspection recovery contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from langchain_core.messages import AIMessage, ToolMessage

from assistant_agent.coding.models import (
    CodingInspectCallEvidence,
    CodingInspectProgress,
    CodingInspectRecoveryAttempt,
)

MAX_INSPECT_EPOCHS = 3


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_inspect_progress_digest(progress: Mapping[str, object]) -> str:
    calls = tuple(
        CodingInspectCallEvidence.model_validate(call)
        for call in progress.get("calls", ())
    )
    payload = {
        "schema_version": 1,
        "reason": progress.get("reason"),
        "base_commit": progress.get("base_commit"),
        "workspace_diff_digest": progress.get("workspace_diff_digest"),
        "calls": [
            call.model_dump(mode="json")
            for call in sorted(
                calls,
                key=lambda item: (
                    item.tool_name,
                    item.arguments_digest,
                    item.result_digest,
                    item.relative_paths,
                ),
            )
        ],
    }
    return _digest(payload)


def validate_inspect_recovery_history(
    values: Sequence[CodingInspectRecoveryAttempt | Mapping[str, object]],
) -> tuple[CodingInspectRecoveryAttempt, ...]:
    history = tuple(CodingInspectRecoveryAttempt.model_validate(item) for item in values)
    if len(history) > MAX_INSPECT_EPOCHS:
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    if tuple(item.epoch for item in history) != tuple(range(1, len(history) + 1)):
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    digests = tuple(item.progress.progress_digest for item in history)
    if len(digests) != len(set(digests)):
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    if any(item.outcome == "retrying" for item in history[:-1]):
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    if any(item.outcome in {"no_progress", "exhausted"} for item in history[:-1]):
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    return history


def _canonical_path(value: object) -> str | None:
    if type(value) is not str or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _argument_paths(args: object) -> tuple[str, ...]:
    if not isinstance(args, Mapping):
        return ()
    raw: list[object] = []
    if "path" in args:
        raw.append(args["path"])
    paths = args.get("paths")
    if isinstance(paths, (list, tuple)):
        raw.extend(paths)
    return tuple(sorted({path for value in raw if (path := _canonical_path(value))}))[:32]


def extract_inspect_progress(
    result: Mapping[str, object],
    *,
    epoch: int,
    base_commit: str,
    workspace_diff_digest: str,
    read_tool_names: frozenset[str],
    model_call_limit: int,
    tool_call_limit: int,
) -> CodingInspectProgress | None:
    messages = tuple(result.get("messages", ()))
    if any(
        isinstance(message, ToolMessage)
        and message.name == "coding_propose_patch"
        and isinstance(message.artifact, dict)
        for message in messages
    ):
        return None
    tool_counts = result.get("run_tool_call_count")
    tool_count = tool_counts.get("__all__", 0) if isinstance(tool_counts, Mapping) else 0
    model_count = result.get("run_model_call_count", 0)
    if type(tool_count) is int and tool_count > tool_call_limit:
        reason: Literal["tool_budget_exhausted", "model_budget_exhausted"] = (
            "tool_budget_exhausted"
        )
    elif type(model_count) is int and model_count >= model_call_limit:
        reason = "model_budget_exhausted"
    else:
        return None

    calls_by_id: dict[str, tuple[str, object]] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call.get("name") in read_tool_names and isinstance(call.get("id"), str):
                calls_by_id[call["id"]] = (str(call["name"]), call.get("args", {}))
    evidence: list[CodingInspectCallEvidence] = []
    for message in messages:
        if (
            not isinstance(message, ToolMessage)
            or message.tool_call_id not in calls_by_id
            or getattr(message, "status", "success") == "error"
        ):
            continue
        tool_name, args = calls_by_id[message.tool_call_id]
        evidence.append(
            CodingInspectCallEvidence(
                tool_name=tool_name,
                arguments_digest=_digest(args),
                result_digest=_digest(message.content),
                relative_paths=_argument_paths(args),
            )
        )
    calls = tuple(
        sorted(
            {(
                item.tool_name,
                item.arguments_digest,
                item.result_digest,
                item.relative_paths,
            ): item for item in evidence}.values(),
            key=lambda item: (item.tool_name, item.arguments_digest, item.result_digest),
        )
    )[:64]
    value = {
        "epoch": epoch,
        "reason": reason,
        "base_commit": base_commit,
        "workspace_diff_digest": workspace_diff_digest,
        "calls": calls,
    }
    return CodingInspectProgress(
        **value,
        progress_digest=canonical_inspect_progress_digest(value),
    )


def render_inspect_recovery_context(
    values: Sequence[CodingInspectRecoveryAttempt | Mapping[str, object]],
) -> str:
    history = tuple(CodingInspectRecoveryAttempt.model_validate(item) for item in values)
    tools = sorted({call.tool_name for item in history for call in item.progress.calls})[:12]
    paths = sorted({path for item in history for call in item.progress.calls for path in call.relative_paths})[:32]
    return (
        "Primary inspection reached its bounded read budget. Continue from prior "
        "canonical evidence, avoid repeating reads, inspect only missing context, and "
        "produce one complete coding_propose_patch proposal.\n"
        f"Previously used read tools: {', '.join(tools) or '(none)'}.\n"
        f"Previously inspected paths: {', '.join(paths) or '(none)'}。"
    )


@dataclass(frozen=True)
class InspectRecoveryOutcome:
    status: Literal["retrying", "no_progress", "exhausted"]
    history: tuple[CodingInspectRecoveryAttempt, ...]
    next_epoch: int | None = None
    error_code: str | None = None


def evaluate_inspect_recovery(
    progress: CodingInspectProgress | Mapping[str, object],
    values: Sequence[CodingInspectRecoveryAttempt | Mapping[str, object]],
) -> InspectRecoveryOutcome:
    current = CodingInspectProgress.model_validate(progress)
    history = validate_inspect_recovery_history(values)
    if current.epoch != len(history) + 1:
        raise ValueError("coding_inspect_recovery_binding_mismatch")
    if history:
        previous = history[-1].progress
        previous_keys = {
            (call.tool_name, call.arguments_digest, call.result_digest)
            for call in previous.calls
        }
        current_keys = {
            (call.tool_name, call.arguments_digest, call.result_digest)
            for call in current.calls
        }
        previous_paths = {path for call in previous.calls for path in call.relative_paths}
        current_paths = {path for call in current.calls for path in call.relative_paths}
        if (
            current.progress_digest == previous.progress_digest
            or current_keys <= previous_keys
            or current_paths <= previous_paths
        ):
            if current.progress_digest == previous.progress_digest:
                return InspectRecoveryOutcome(
                    status="no_progress",
                    history=(
                        *history[:-1],
                        history[-1].model_copy(update={"outcome": "no_progress"}),
                    ),
                    error_code="coding_inspect_no_progress",
                )
            attempt = CodingInspectRecoveryAttempt(
                epoch=current.epoch, progress=current, outcome="no_progress"
            )
            return InspectRecoveryOutcome(
                status="no_progress",
                history=(*history[:-1], history[-1].model_copy(update={"outcome": "completed"}), attempt),
                error_code="coding_inspect_no_progress",
            )
    if current.epoch >= MAX_INSPECT_EPOCHS:
        attempt = CodingInspectRecoveryAttempt(
            epoch=current.epoch, progress=current, outcome="exhausted"
        )
        return InspectRecoveryOutcome(
            status="exhausted",
            history=(*history[:-1], history[-1].model_copy(update={"outcome": "completed"}), attempt) if history else (attempt,),
            error_code="coding_inspect_recovery_exhausted",
        )
    normalized = (
        (*history[:-1], history[-1].model_copy(update={"outcome": "completed"}))
        if history
        else ()
    )
    attempt = CodingInspectRecoveryAttempt(
        epoch=current.epoch, progress=current, outcome="retrying"
    )
    return InspectRecoveryOutcome(
        status="retrying", history=(*normalized, attempt), next_epoch=current.epoch + 1
    )


def validate_inspect_recovery_checkpoint(
    state: Mapping[str, object],
    *,
    base_commit: str,
    workspace_diff_digest: str,
) -> tuple[CodingInspectRecoveryAttempt, ...]:
    try:
        epoch = state.get("inspect_epoch", 1)
        status = state.get("inspect_recovery_status")
        progress_value = state.get("inspect_progress")
        consumed = state.get("inspect_recovery_context_consumed", False)
        history = validate_inspect_recovery_history(
            state.get("inspect_recovery_history", ())  # type: ignore[arg-type]
        )
        if type(epoch) is not int or not 1 <= epoch <= MAX_INSPECT_EPOCHS:
            raise ValueError
        if type(consumed) is not bool:
            raise ValueError
        progress = (
            CodingInspectProgress.model_validate(progress_value)
            if progress_value is not None
            else None
        )
        all_progress = [*(item.progress for item in history), *(() if progress is None else (progress,))]
        if any(
            item.base_commit != base_commit
            or item.workspace_diff_digest != workspace_diff_digest
            or item.progress_digest
            != canonical_inspect_progress_digest(item.model_dump())
            for item in all_progress
        ):
            raise ValueError
        if status is None:
            if epoch != 1 or progress is not None or history or consumed:
                raise ValueError
        elif status == "pending":
            if progress is None or progress.epoch != epoch or consumed:
                raise ValueError
        elif status == "retrying":
            if progress is not None or not history or history[-1].outcome != "retrying" or epoch != history[-1].epoch + 1:
                raise ValueError
        elif status in {"completed", "no_progress", "exhausted"}:
            if (
                progress is not None
                or consumed
                or not history
                or history[-1].outcome != status
            ):
                raise ValueError
        else:
            raise ValueError
        return history
    except Exception as exc:
        raise ValueError("coding_inspect_recovery_binding_mismatch") from exc

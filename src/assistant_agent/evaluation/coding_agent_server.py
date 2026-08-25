"""Public Agent Server SDK driver for trusted coding behavior fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

from httpx import HTTPStatusError, TransportError
from langgraph_sdk import get_client

from assistant_agent.agent_server.client import (
    THREAD_GRAPH_METADATA_KEY,
    bind_thread_graph_identity,
    require_current_checkpoint_graph,
    require_thread_graph_identity,
)
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.evaluation.coding_behavior import CodingBehaviorCase
_HEX_40_64 = re.compile(r"^[0-9a-f]{40,64}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TRANSITIONS = 16
_MAX_EVIDENCE_BYTES = 16_384


class _Threads(Protocol):
    async def create(self, **kwargs: Any) -> Mapping[str, Any]: ...
    async def get(self, thread_id: str) -> Mapping[str, Any]: ...
    async def get_state(self, thread_id: str, *, subgraphs: bool = False) -> Mapping[str, Any]: ...
    async def delete(self, thread_id: str) -> None: ...


class _Runs(Protocol):
    async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> Mapping[str, Any]: ...
    async def join(self, thread_id: str, run_id: str) -> object: ...
    async def cancel(self, thread_id: str, run_id: str, **kwargs: Any) -> None: ...
    async def get(self, thread_id: str, run_id: str) -> Mapping[str, Any]: ...
    async def list(self, thread_id: str, **kwargs: Any) -> Sequence[Mapping[str, Any]]: ...


class CodingBehaviorAgentServerClient(Protocol):
    threads: _Threads
    runs: _Runs


class FixtureCapability(Protocol):
    base_commit: str


class FixtureLease(Protocol):
    def validate(self) -> None: ...
    def close(self) -> None: ...


class FixtureStore(Protocol):
    def resolve(
        self, fixture: FixtureCapability, case: CodingBehaviorCase
    ) -> FixtureLease: ...


@dataclass(frozen=True, slots=True)
class CodingBehaviorTransitionEvidence:
    sequence: int
    kind: Literal["patch_approval", "coding_review_decision", "merge_approval", "terminal"]
    checkpoint_digest: str


@dataclass(frozen=True, slots=True)
class CodingBehaviorDriverResult:
    status: Literal["completed", "failed"]
    terminal_status: str | None
    error_code: str | None
    failure_category: Literal["none", "configuration", "governance", "permission", "transport", "cancelled", "deadline", "terminal"]
    thread_digest: str
    run_digests: tuple[str, ...]
    interrupt_kinds: tuple[str, ...]
    interrupt_count: int
    transitions: tuple[CodingBehaviorTransitionEvidence, ...]
    elapsed_ms: int
    final_commit: str | None = None
    validation_tree_digest: str | None = None
    review_tree_digest: str | None = None
    integration_tree_digest: str | None = None
    cleanup_pending: bool = False


_DEBT_ISSUER = object()


class ThreadCleanupDebt:
    """Opaque, process-local capability for retrying one exact thread cleanup."""

    __slots__ = (
        "_client",
        "_thread_id",
        "_thread_digest",
        "_identity_digest",
        "_released",
    )

    def __init__(
        self,
        *,
        client: CodingBehaviorAgentServerClient,
        thread_id: str,
        identity: str,
        issuer: object,
    ) -> None:
        if issuer is not _DEBT_ISSUER:
            raise TypeError("thread cleanup debt is store-issued only")
        self._client = client
        self._thread_id = thread_id
        self._thread_digest = _digest(thread_id)
        self._identity_digest = _digest(identity)
        self._released = False

    async def aretry(self) -> bool:
        if self._released:
            return True
        self._released = await _delete_and_confirm_thread(
            self._client,
            self._thread_id,
        )
        return self._released

    def retry(self) -> bool:
        return asyncio.run(self.aretry())

    def __reduce__(self) -> object:
        raise TypeError("thread cleanup debt is not serializable")

    def __repr__(self) -> str:
        return (
            "ThreadCleanupDebt(thread_digest="
            f"{self._thread_digest!r}, identity_digest={self._identity_digest!r}, "
            f"released={self._released!r})"
        )


@dataclass(frozen=True, slots=True)
class DriverOutcome:
    """Public redacted result plus an optional non-serializable cleanup capability."""

    result: CodingBehaviorDriverResult
    _cleanup_debt: ThreadCleanupDebt | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self.result, name)

    @property
    def cleanup_debt(self) -> ThreadCleanupDebt | None:
        return self._cleanup_debt

    def model_dump(self) -> dict[str, object]:
        return {"result": asdict(self.result)}

    def __repr__(self) -> str:
        return (
            "DriverOutcome(status="
            f"{self.result.status!r}, error_code={self.result.error_code!r}, "
            f"thread_digest={self.result.thread_digest!r}, "
            f"cleanup_pending={self.result.cleanup_pending!r})"
        )


class _UnknownRunOutcome(RuntimeError):
    pass


class _RunCleanupPending(RuntimeError):
    pass


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _model_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json")
        return _mapping(result, label="state model")
    return {}


class FixtureApprovalPolicy:
    """Approve only interrupts bound to one live store-issued fixture capability."""

    def __init__(
        self,
        *,
        store: FixtureStore,
        case: CodingBehaviorCase,
        fixture: FixtureCapability,
        repository_id: str,
        identity: str,
        target_branch: str,
    ) -> None:
        if not repository_id.strip() or not identity.strip() or target_branch != "main":
            raise ValueError("fixture approval policy requires canonical bindings")
        try:
            lease = store.resolve(fixture, case)
        except Exception as exc:
            raise ValueError("fixture approval requires a store-issued binding") from exc
        lease.close()
        self.store = store
        self.case = case
        self.fixture = fixture
        self.repository_id = repository_id
        self.identity = identity
        self.target_branch = target_branch

    def _require_live_fixture(self) -> None:
        try:
            lease = self.store.resolve(self.fixture, self.case)
            lease.validate()
        except Exception as exc:
            raise ValueError("fixture approval binding is no longer live") from exc
        finally:
            if "lease" in locals():
                lease.close()

    def response(self, kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_live_fixture()
        if kind in {"patch_approval", "coding_review_decision"} and payload.get("base_commit") != self.fixture.base_commit:
            raise ValueError("interrupt base commit does not match fixture")
        if kind == "patch_approval":
            required = {"action", "workspace_ref", "base_commit", "patch_digest", "changed_paths", "summary", "diff_preview", "origin"}
            if set(payload) != required or payload.get("action") != "coding_patch_apply" or not _HEX_64.fullmatch(str(payload.get("patch_digest", ""))):
                raise ValueError("patch interrupt schema is invalid")
            paths = payload.get("changed_paths")
            if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
                raise ValueError("patch interrupt path inventory is invalid")
            if len(set(paths)) != len(paths) or paths != sorted(paths) or not set(paths).issubset(self.case.allowed_changed_paths):
                raise ValueError("patch interrupt exceeds the fixture path scope")
            return {"decision": "approve", "patch_digest": payload["patch_digest"]}
        if kind == "coding_review_decision":
            required = {
                "action", "review_generation", "workspace_ref", "base_commit", "snapshot_ref", "tree_digest",
                "workspace_diff_digest", "snapshot_materialization_schema_version", "snapshot_created_at", "snapshot_expires_at",
                "patch_digest", "validation_digest", "report_digest", "review_repair_count", "review_repair_history_digest",
                "review_status", "finding_count", "findings_summary",
            }
            if set(payload) != required or payload.get("action") != "coding_review_decision" or payload.get("review_status") not in {"clean", "findings"}:
                raise ValueError("review interrupt schema is invalid")
            findings = payload.get("findings_summary")
            if (
                type(payload.get("review_generation")) is not int
                or type(payload.get("review_repair_count")) is not int
                or not 0 <= payload["review_repair_count"] <= 2
                or type(payload.get("finding_count")) is not int
                or not isinstance(findings, (list, tuple))
                or len(findings) > 12
                or payload["finding_count"] < len(findings)
            ):
                raise ValueError("review interrupt findings are invalid")
            for key in ("workspace_diff_digest", "patch_digest", "validation_digest", "report_digest", "review_repair_history_digest"):
                if not _HEX_64.fullmatch(str(payload.get(key, ""))):
                    raise ValueError("review interrupt digest binding is invalid")
            if not _HEX_40_64.fullmatch(str(payload.get("tree_digest", ""))):
                raise ValueError("review interrupt tree binding is invalid")
            for finding in findings:
                if not isinstance(finding, Mapping) or set(finding) != {"finding_id", "severity", "category", "title", "path", "line"}:
                    raise ValueError("review interrupt finding summary is invalid")
                if any(not isinstance(finding[key], str) for key in ("finding_id", "severity", "category", "title", "path")) or type(finding["line"]) is not int:
                    raise ValueError("review interrupt finding summary is invalid")
            response = {key: value for key, value in payload.items() if key not in {"action", "review_status", "finding_count", "findings_summary"}}
            response["decision"] = "approve"
            return response
        if kind == "merge_approval":
            required = {"action", "source_commit", "expected_target_head", "target_branch", "strategy", "result_commit", "result_tree", "merge_preview_digest"}
            if set(payload) != required or payload.get("action") != "coding_merge_apply" or payload.get("target_branch") != self.target_branch:
                raise ValueError("merge interrupt schema is invalid")
            if payload.get("expected_target_head") != self.fixture.base_commit:
                raise ValueError("merge interrupt target head does not match fixture base")
            for key in ("source_commit", "expected_target_head", "result_commit", "result_tree"):
                if not _HEX_40_64.fullmatch(str(payload.get(key, ""))):
                    raise ValueError("merge interrupt object binding is invalid")
            if not _HEX_64.fullmatch(str(payload.get("merge_preview_digest", ""))):
                raise ValueError("merge interrupt digest is invalid")
            return {"decision": "approve", "source_commit": payload["source_commit"], "expected_target_head": payload["expected_target_head"], "merge_preview_digest": payload["merge_preview_digest"]}
        raise ValueError("interrupt is not auto-approvable")


class CodingBehaviorAgentServerDriver:
    """Drive native coding runs without owning or reproducing their state machine."""

    def __init__(
        self,
        *,
        client: CodingBehaviorAgentServerClient | None = None,
        server_url: str = "http://127.0.0.1:8089",
        identity: str | None = None,
        max_interrupts: int = 3,
        clock: Callable[[], float] | None = None,
        expected_execution_attestation_digest: str | None = None,
    ) -> None:
        if max_interrupts < 0 or max_interrupts > _MAX_TRANSITIONS:
            raise ValueError("interrupt budget is invalid")
        self.client = client or get_client(
            url=server_url,
            headers={"x-assistant-user": identity} if identity else None,
        )
        self.max_interrupts = max_interrupts
        self.clock = clock or monotonic
        if expected_execution_attestation_digest is not None and not _HEX_64.fullmatch(
            expected_execution_attestation_digest
        ):
            raise ValueError("execution attestation digest is invalid")
        self.expected_execution_attestation_digest = expected_execution_attestation_digest

    def run(self, *, case: CodingBehaviorCase, policy: FixtureApprovalPolicy) -> DriverOutcome:
        """Run from a synchronous CLI or runner boundary."""

        return asyncio.run(self.arun(case=case, policy=policy))

    async def arun(
        self, *, case: CodingBehaviorCase, policy: FixtureApprovalPolicy
    ) -> DriverOutcome:
        started = self.clock()
        deadline = started + case.max_runtime_seconds
        thread_id = ""
        current_run_id: str | None = None
        run_ids: list[str] = []
        kinds: list[str] = []
        seen_interrupt_ids: set[str] = set()
        seen_payload_digests: set[str] = set()
        consumed_checkpoint_ids: set[str] = set()
        transitions: list[CodingBehaviorTransitionEvidence] = []
        workspace_ref: str | None = None

        def elapsed_ms() -> int:
            return max(0, min(3_600_000, int((self.clock() - started) * 1000)))

        def preflight_failed(code: str, category: str) -> DriverOutcome:
            return DriverOutcome(CodingBehaviorDriverResult(
                status="failed",
                terminal_status=None,
                error_code=code,
                failure_category=category,  # type: ignore[arg-type]
                thread_digest=_digest("uncreated"),
                run_digests=(),
                interrupt_kinds=(),
                interrupt_count=0,
                transitions=(),
                elapsed_ms=elapsed_ms(),
            ))

        if case != policy.case:
            return preflight_failed("coding_eval_case_invalid", "governance")

        async def bounded(factory: Callable[[], Any]) -> Any:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(factory(), timeout=remaining)

        async def release(result: CodingBehaviorDriverResult) -> DriverOutcome:
            if not thread_id:
                return DriverOutcome(result)
            if await _delete_and_confirm_thread(self.client, thread_id):
                return DriverOutcome(replace(result, cleanup_pending=False))
            failed_result = replace(
                result,
                status="failed",
                error_code="coding_eval_cleanup_pending",
                failure_category="cancelled",
                cleanup_pending=True,
            )
            return DriverOutcome(
                failed_result,
                ThreadCleanupDebt(
                    client=self.client,
                    thread_id=thread_id,
                    identity=policy.identity,
                    issuer=_DEBT_ISSUER,
                ),
            )

        async def failed(
            code: str, category: str, *, cleanup_pending: bool = False
        ) -> DriverOutcome:
            nonlocal current_run_id
            if current_run_id and thread_id:
                try:
                    await asyncio.wait_for(
                        self.client.runs.cancel(
                            thread_id,
                            current_run_id,
                            wait=True,
                            action="interrupt",
                        ),
                        timeout=5.0,
                    )
                    cancelled = await asyncio.wait_for(
                        self.client.runs.get(thread_id, current_run_id),
                        timeout=5.0,
                    )
                    if cancelled.get("run_id") != current_run_id or cancelled.get(
                        "status"
                    ) in {"pending", "running", None}:
                        raise ValueError("cancelled run did not reach a terminal status")
                except Exception:
                    category = "cancelled"
                    cleanup_pending = True
                current_run_id = None
            result = CodingBehaviorDriverResult(
                status="failed", terminal_status=None, error_code=code,
                failure_category=category,  # type: ignore[arg-type]
                thread_digest=_digest(thread_id) if thread_id else _digest("uncreated"),
                run_digests=tuple(_digest(value) for value in run_ids),
                interrupt_kinds=tuple(kinds), interrupt_count=len(kinds),
                transitions=tuple(transitions), elapsed_ms=elapsed_ms(),
                cleanup_pending=cleanup_pending,
            )
            return await release(result)

        async def list_runs(
            awaiter: Callable[[Callable[[], Any]], Any],
        ) -> dict[str, str]:
            raw = await awaiter(
                lambda: self.client.runs.list(
                    thread_id,
                    limit=100,
                    offset=0,
                    select=["run_id", "status"],
                )
            )
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise ValueError("run inventory is invalid")
            if len(raw) >= 100:
                raise ValueError("exclusive thread run inventory exceeded its budget")
            inventory: dict[str, str] = {}
            for item in raw:
                run = _mapping(item, label="run inventory item")
                run_id = run.get("run_id")
                status = run.get("status")
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or run_id in inventory
                    or status
                    not in {
                        "pending",
                        "running",
                        "error",
                        "success",
                        "timeout",
                        "interrupted",
                    }
                ):
                    raise ValueError("run inventory item is invalid")
                inventory[run_id] = status
            return inventory

        async def cancel_and_confirm(run_ids_to_cancel: Sequence[str]) -> None:
            for run_id in run_ids_to_cancel:
                await asyncio.wait_for(
                    self.client.runs.cancel(
                        thread_id,
                        run_id,
                        wait=True,
                        action="interrupt",
                    ),
                    timeout=5.0,
                )
                run = await asyncio.wait_for(
                    self.client.runs.get(thread_id, run_id),
                    timeout=5.0,
                )
                if run.get("run_id") != run_id or run.get("status") in {
                    "pending",
                    "running",
                    None,
                }:
                    raise _RunCleanupPending(
                        "cancelled run remained active or unverifiable"
                    )

        async def reconcile_attempt(
            baseline: set[str],
            callback_ids: Sequence[str],
            response_id: str | None,
            *,
            timed_out: bool,
            transport_failed: bool,
        ) -> str:
            nonlocal current_run_id
            async def cleanup_await(factory: Callable[[], Any]) -> Any:
                return await asyncio.wait_for(factory(), timeout=5.0)

            try:
                inventory = await list_runs(cleanup_await)
            except Exception as exc:
                known = tuple(
                    sorted(
                        {
                            value
                            for value in (*callback_ids, response_id)
                            if isinstance(value, str) and value
                        }
                    )
                )
                cleanup_succeeded = False
                try:
                    await cancel_and_confirm(known)
                    await asyncio.wait_for(
                        self.client.threads.delete(thread_id), timeout=5.0
                    )
                    cleanup_succeeded = True
                except Exception:
                    pass
                if cleanup_succeeded:
                    raise _UnknownRunOutcome(
                        "exclusive thread inventory failed but thread cleanup completed"
                    ) from exc
                raise _RunCleanupPending(
                    "exclusive thread run inventory could not be reconciled"
                ) from exc
            new_ids = tuple(sorted(set(inventory).difference(baseline)))
            if any(not value for value in callback_ids):
                active = tuple(
                    run_id
                    for run_id in new_ids
                    if inventory[run_id] in {"pending", "running"}
                )
                await cancel_and_confirm(active)
                current_run_id = None
                raise _UnknownRunOutcome("run callback identity was malformed")
            claimed = {
                value
                for value in (*callback_ids, response_id)
                if isinstance(value, str) and value
            }
            if claimed and not claimed.issubset(new_ids):
                active = tuple(
                    run_id
                    for run_id in new_ids
                    if inventory[run_id] in {"pending", "running"}
                )
                await cancel_and_confirm(active)
                if current_run_id in active:
                    current_run_id = None
                raise _UnknownRunOutcome("run callback/response identity mismatch")
            if len(new_ids) != 1:
                active = tuple(
                    run_id
                    for run_id in new_ids
                    if inventory[run_id] in {"pending", "running"}
                )
                await cancel_and_confirm(active)
                current_run_id = None
                if not new_ids and timed_out:
                    raise TimeoutError
                if not new_ids and transport_failed:
                    raise ConnectionError("run create transport failed before discovery")
                raise _UnknownRunOutcome(
                    "exclusive thread produced a non-unique run attempt"
                )
            run_id = new_ids[0]
            if run_id in run_ids:
                raise _UnknownRunOutcome("server reused a prior run identity")
            if inventory[run_id] in {"pending", "running"}:
                await cancel_and_confirm((run_id,))
                if current_run_id == run_id:
                    current_run_id = None
                if timed_out:
                    raise TimeoutError
                if transport_failed:
                    raise ConnectionError(
                        "run create transport failed after cancellation confirmation"
                    )
                raise _UnknownRunOutcome("run attempt did not reach a terminal status")
            return run_id

        async def start_run(**kwargs: Any) -> None:
            nonlocal current_run_id
            baseline = set(await list_runs(bounded))
            callback_ids: list[str] = []
            response_id: str | None = None
            timed_out = False
            transport_failed = False
            attempt_id = str(uuid4())
            request = dict(kwargs)
            request_metadata = dict(request.pop("metadata", {}) or {})
            request_metadata["coding_eval_attempt_id"] = attempt_id

            def created(metadata: Mapping[str, Any]) -> None:
                run_id = metadata.get("run_id")
                callback_thread_id = metadata.get("thread_id")
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or callback_thread_id not in {None, thread_id}
                ):
                    callback_ids.append("")
                    return
                callback_ids.append(run_id)

            try:
                run = await bounded(
                    lambda: self.client.runs.create(
                        thread_id,
                        ASSISTANT_GRAPH_ID,
                        metadata=request_metadata,
                        multitask_strategy="reject",
                        on_run_created=created,
                        **request,
                    )
                )
                response_value = _mapping(run, label="run").get("run_id")
                response_id = (
                    response_value if isinstance(response_value, str) else None
                )
                current_run_id = response_id or next(
                    (value for value in callback_ids if value), None
                )
                if current_run_id is not None:
                    await bounded(
                        lambda: self.client.runs.join(thread_id, current_run_id)
                    )
            except TimeoutError:
                timed_out = True
            except (ConnectionError, OSError, TransportError):
                transport_failed = True
            value = await reconcile_attempt(
                baseline,
                callback_ids,
                response_id,
                timed_out=timed_out,
                transport_failed=transport_failed,
            )
            current_run_id = value
            terminal_run = await asyncio.wait_for(
                self.client.runs.get(thread_id, value), timeout=5.0
            )
            terminal_metadata = terminal_run.get("metadata")
            if terminal_run.get("run_id") != value or terminal_run.get("status") in {
                "pending",
                "running",
                None,
            } or not isinstance(terminal_metadata, Mapping) or terminal_metadata.get(
                "coding_eval_attempt_id"
            ) != attempt_id:
                raise _UnknownRunOutcome("reconciled run is not terminal")
            run_ids.append(value)
            current_run_id = None

        try:
            metadata = bind_thread_graph_identity(
                {"coding_eval_identity": policy.identity, "coding_eval_repo_id": policy.repository_id, "coding_eval_case_id": case.case_id},
                expected_graph_id=ASSISTANT_GRAPH_ID,
            )
            thread_id = str(uuid4())
            thread = await bounded(
                lambda: self.client.threads.create(
                    metadata=metadata,
                    thread_id=thread_id,
                    if_exists="raise",
                    graph_id=ASSISTANT_GRAPH_ID,
                )
            )
            require_thread_graph_identity(thread, expected_graph_id=ASSISTANT_GRAPH_ID)
            if thread.get("thread_id") != thread_id or not _thread_binding_matches(
                thread,
                identity=policy.identity,
                repository_id=policy.repository_id,
                case_id=case.case_id,
            ):
                return await failed("coding_eval_repository_not_bound", "permission")
            await start_run(
                input={"messages": [{"role": "user", "content": case.request}], "execution_mode": "coding", "coding_repo_id": policy.repository_id},
                context={"entry_profile": "evaluation", "assistant_execution_mode": "coding"},
                metadata={"coding_eval_case_id": case.case_id},
            )
            while True:
                if self.clock() >= deadline:
                    return await failed("coding_eval_deadline_exceeded", "deadline")
                thread = await bounded(lambda: self.client.threads.get(thread_id))
                if thread.get("thread_id") != thread_id or not _thread_binding_matches(
                    thread,
                    identity=policy.identity,
                    repository_id=policy.repository_id,
                    case_id=case.case_id,
                ):
                    return await failed("coding_eval_repository_not_bound", "permission")
                require_thread_graph_identity(thread, expected_graph_id=ASSISTANT_GRAPH_ID)
                state = await bounded(
                    lambda: self.client.threads.get_state(thread_id, subgraphs=True)
                )
                require_current_checkpoint_graph(state)
                values = _mapping(state.get("values"), label="checkpoint values")
                current_workspace_ref = values.get("workspace_ref")
                if (
                    self.expected_execution_attestation_digest is not None
                    and values.get("execution_attestation_digest")
                    != self.expected_execution_attestation_digest
                ):
                    return await failed(
                        "coding_eval_configuration_error", "configuration"
                    )
                if (
                    values.get("execution_mode") != "coding"
                    or values.get("coding_repo_id") != policy.repository_id
                    or values.get("base_commit") != policy.fixture.base_commit
                    or not isinstance(current_workspace_ref, str)
                    or not current_workspace_ref
                    or _state_thread_id(state) != thread_id
                    or _state_owner(state) != policy.identity
                ):
                    return await failed("coding_eval_repository_not_bound", "permission")
                if workspace_ref is None:
                    workspace_ref = current_workspace_ref
                elif current_workspace_ref != workspace_ref:
                    return await failed("coding_eval_repository_not_bound", "permission")
                try:
                    checkpoint_id = _checkpoint_id(state)
                except (TypeError, ValueError):
                    return await failed("coding_eval_unknown_interrupt", "governance")
                if checkpoint_id in consumed_checkpoint_ids:
                    return await failed("coding_eval_unknown_interrupt", "governance")
                try:
                    interrupt = _single_interrupt(state)
                except (TypeError, ValueError):
                    return await failed("coding_eval_unknown_interrupt", "governance")
                if interrupt is None:
                    terminal = _model_mapping(values.get("coding_result"))
                    terminal_status = terminal.get("status")
                    if not isinstance(terminal_status, str):
                        return await failed("coding_eval_terminal_mismatch", "terminal")
                    if tuple(kinds) != case.required_interrupts:
                        return await failed("coding_eval_terminal_mismatch", "governance")
                    if terminal.get("base_commit") != policy.fixture.base_commit:
                        return await failed("coding_eval_repository_not_bound", "permission")
                    transitions.append(_transition(len(transitions) + 1, "terminal", state))
                    return await release(CodingBehaviorDriverResult(
                        status="completed", terminal_status=terminal_status, error_code=None, failure_category="none",
                        thread_digest=_digest(thread_id), run_digests=tuple(_digest(value) for value in run_ids),
                        interrupt_kinds=tuple(kinds), interrupt_count=len(kinds), transitions=tuple(transitions), elapsed_ms=elapsed_ms(),
                        final_commit=_optional_object_id(terminal.get("result_commit")),
                        validation_tree_digest=_nested_object_id(values, "validation_snapshot", "tree_digest"),
                        review_tree_digest=_nested_object_id(values, "review_report", "tree_digest"),
                        integration_tree_digest=_nested_object_id(values, "merge_result", "result_tree"),
                    ))
                interrupt_id, payload = interrupt
                try:
                    payload_digest = _payload_digest(payload)
                except (TypeError, ValueError):
                    return await failed("coding_eval_unknown_interrupt", "governance")
                if (
                    interrupt_id in seen_interrupt_ids
                    or payload_digest in seen_payload_digests
                    or checkpoint_id in consumed_checkpoint_ids
                ):
                    return await failed("coding_eval_unknown_interrupt", "governance")
                seen_interrupt_ids.add(interrupt_id)
                seen_payload_digests.add(payload_digest)
                consumed_checkpoint_ids.add(checkpoint_id)
                kind = _interrupt_kind(payload)
                if kind is None:
                    try:
                        await start_run(
                            command={"resume": {"decision": "reject"}},
                            checkpoint_id=checkpoint_id,
                            context={"entry_profile": "evaluation", "assistant_execution_mode": "coding"},
                        )
                    except Exception:
                        return await failed(
                            "coding_eval_unknown_interrupt", "governance"
                        )
                    return await failed("coding_eval_unknown_interrupt", "governance")
                if len(kinds) >= self.max_interrupts:
                    return await failed("coding_eval_interrupt_budget_exceeded", "governance")
                expected = case.required_interrupts[len(kinds)] if len(kinds) < len(case.required_interrupts) else None
                if kind != expected:
                    return await failed("coding_eval_unknown_interrupt", "governance")
                if payload.get("workspace_ref") is not None and payload.get("workspace_ref") != workspace_ref:
                    return await failed("coding_eval_repository_not_bound", "permission")
                try:
                    response = policy.response(kind, payload)
                except ValueError:
                    try:
                        await start_run(
                            command={"resume": {"decision": "reject"}},
                            checkpoint_id=checkpoint_id,
                            context={"entry_profile": "evaluation", "assistant_execution_mode": "coding"},
                        )
                    except Exception:
                        return await failed(
                            "coding_eval_unknown_interrupt", "governance"
                        )
                    return await failed("coding_eval_unknown_interrupt", "governance")
                kinds.append(kind)
                transitions.append(_transition(len(transitions) + 1, kind, state))
                if _evidence_size(transitions) > _MAX_EVIDENCE_BYTES:
                    return await failed("coding_eval_interrupt_budget_exceeded", "governance")
                await start_run(
                    command={"resume": response},
                    checkpoint_id=checkpoint_id,
                    context={"entry_profile": "evaluation", "assistant_execution_mode": "coding"},
                )
        except _RunCleanupPending:
            return await failed(
                "coding_eval_unknown_run_outcome",
                "governance",
            )
        except _UnknownRunOutcome:
            return await failed(
                "coding_eval_unknown_run_outcome",
                "governance",
            )
        except TimeoutError:
            return await failed("coding_eval_deadline_exceeded", "deadline")
        except (ConnectionError, OSError, TransportError):
            return await failed("coding_eval_server_unavailable", "transport")
        except HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                category = "permission"
                code = "coding_eval_repository_not_bound"
            elif exc.response.status_code == 409:
                category = "configuration"
                code = "coding_eval_configuration_error"
            else:
                category = "transport"
                code = "coding_eval_server_unavailable"
            return await failed(code, category)
        except PermissionError:
            return await failed("coding_eval_repository_not_bound", "permission")
        except Exception:
            return await failed("coding_eval_terminal_mismatch", "terminal")

async def _delete_and_confirm_thread(
    client: CodingBehaviorAgentServerClient,
    thread_id: str,
) -> bool:
    try:
        await asyncio.wait_for(client.threads.delete(thread_id), timeout=5.0)
    except KeyError:
        return True
    except HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return True
        return False
    except Exception:
        return False
    try:
        await asyncio.wait_for(client.threads.get(thread_id), timeout=5.0)
    except KeyError:
        return True
    except HTTPStatusError as exc:
        return exc.response.status_code == 404
    except Exception:
        return False
    return False


def _thread_owner(thread: Mapping[str, Any]) -> object:
    metadata = thread.get("metadata")
    return metadata.get("owner") if isinstance(metadata, Mapping) else None


def _thread_binding_matches(
    thread: Mapping[str, Any],
    *,
    identity: str,
    repository_id: str,
    case_id: str,
) -> bool:
    metadata = thread.get("metadata")
    return isinstance(metadata, Mapping) and metadata == {
        THREAD_GRAPH_METADATA_KEY: ASSISTANT_GRAPH_ID,
        "owner": identity,
        "coding_eval_identity": identity,
        "coding_eval_repo_id": repository_id,
        "coding_eval_case_id": case_id,
    }


def _state_thread_id(state: Mapping[str, Any]) -> object:
    config = state.get("config")
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    return configurable.get("thread_id") if isinstance(configurable, Mapping) else None


def _state_owner(state: Mapping[str, Any]) -> object:
    metadata = state.get("metadata")
    return metadata.get("owner") if isinstance(metadata, Mapping) else None


def _single_interrupt(state: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    found: list[object] = []
    tasks = state.get("tasks", ())
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise ValueError("checkpoint tasks are invalid")
    for task in tasks:
        task_map = _mapping(task, label="checkpoint task")
        interrupts = task_map.get("interrupts", ())
        if not isinstance(interrupts, Sequence) or isinstance(interrupts, (str, bytes)):
            raise ValueError("checkpoint interrupts are invalid")
        found.extend(interrupts)
    if not found:
        return None
    if len(found) != 1:
        raise ValueError("checkpoint must expose exactly one interrupt")
    value = _mapping(found[0], label="interrupt")
    if set(value) != {"id", "value"} or not isinstance(value.get("id"), str):
        raise ValueError("interrupt envelope is invalid")
    return value["id"], _mapping(value.get("value"), label="interrupt value")


def _checkpoint_id(state: Mapping[str, Any]) -> str:
    checkpoint = state.get("checkpoint")
    value = checkpoint.get("checkpoint_id") if isinstance(checkpoint, Mapping) else None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("checkpoint identity is missing or invalid")
    return value


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_EVIDENCE_BYTES * 4:
        raise ValueError("interrupt payload exceeds its input budget")
    return sha256(encoded).hexdigest()


def _interrupt_kind(payload: Mapping[str, Any]) -> str | None:
    return {"coding_patch_apply": "patch_approval", "coding_review_decision": "coding_review_decision", "coding_merge_apply": "merge_approval"}.get(payload.get("action"))


def _transition(sequence: int, kind: str, state: Mapping[str, Any]) -> CodingBehaviorTransitionEvidence:
    checkpoint = state.get("checkpoint")
    checkpoint_id = checkpoint.get("checkpoint_id") if isinstance(checkpoint, Mapping) else None
    projection = json.dumps({"checkpoint_id": str(checkpoint_id), "kind": kind, "next": tuple(str(value) for value in state.get("next", ()))}, sort_keys=True, separators=(",", ":"))
    return CodingBehaviorTransitionEvidence(sequence=sequence, kind=kind, checkpoint_digest=_digest(projection))  # type: ignore[arg-type]


def _evidence_size(values: Sequence[CodingBehaviorTransitionEvidence]) -> int:
    return len(json.dumps([value.__dict__ if hasattr(value, "__dict__") else {"sequence": value.sequence, "kind": value.kind, "checkpoint_digest": value.checkpoint_digest} for value in values], separators=(",", ":")).encode("utf-8"))


def _optional_object_id(value: object) -> str | None:
    return str(value) if isinstance(value, str) and _HEX_40_64.fullmatch(value) else None


def _nested_object_id(values: Mapping[str, Any], object_key: str, field: str) -> str | None:
    return _optional_object_id(_model_mapping(values.get(object_key)).get(field))


__all__ = [
    "CodingBehaviorAgentServerClient", "CodingBehaviorAgentServerDriver",
    "CodingBehaviorDriverResult", "CodingBehaviorTransitionEvidence", "DriverOutcome",
    "ThreadCleanupDebt", "FixtureApprovalPolicy",
    "FixtureCapability", "FixtureStore",
]

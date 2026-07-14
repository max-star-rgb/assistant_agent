"""Append-oriented local JSONL registry for improvement review artifacts."""

from __future__ import annotations

from collections.abc import Sequence
import json
import fcntl
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from assistant_agent.schemas.improvement import (
    AllowlistedEvalResult,
    CandidateEvaluationRecord,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEvidence,
    ImprovementOpportunity,
)
from assistant_agent.services.improvement.evidence import (
    validate_evidence_safety,
    validate_prompt_safe_payload,
)


class JsonlImprovementRegistry:
    """Persist versioned review records without applying candidates."""

    def __init__(self, root: Path | str = ".data/improvement_lab") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._issues: list[str] = []

    @property
    def issues(self) -> list[str]:
        return list(self._issues)

    def append_evidence(self, items: Sequence[ImprovementEvidence]) -> int:
        safe = [item for item in items if not validate_evidence_safety(item)]
        if len(safe) != len(items):
            self._issues.append("registry_unsafe_record")
        return self._append_unique("evidence.jsonl", safe, "evidence_id")

    def append_opportunities(self, items: Sequence[ImprovementOpportunity]) -> int:
        safe = self._safe_records(items)
        return self._append_unique("opportunities.jsonl", safe, "opportunity_id")

    def append_candidates(self, items: Sequence[ImprovementCandidate]) -> int:
        safe = [
            item
            for item in items
            if not validate_prompt_safe_payload(item.model_dump(mode="json"))
        ]
        if len(safe) != len(items):
            self._issues.append("registry_unsafe_record")
        return self._append_unique("candidates.jsonl", safe, "candidate_id")

    def append_validation_results(self, items: Sequence[AllowlistedEvalResult]) -> int:
        safe = self._safe_records(items)
        return self._append_unique("validation_results.jsonl", safe, "validation_id")

    def append_candidate_evaluations(self, items: Sequence[CandidateEvaluationRecord]) -> int:
        safe = self._safe_records(items)
        return self._append_unique("candidate_evaluations.jsonl", safe, "evaluation_id")

    def record_decision(self, decision: ImprovementDecision) -> bool:
        safe = self._safe_records([decision])
        return bool(self._append_unique("decisions.jsonl", safe, "decision_id"))

    def _safe_records(self, items: Sequence[BaseModel]) -> list[BaseModel]:
        safe = [
            item
            for item in items
            if not validate_prompt_safe_payload(item.model_dump(mode="json"))
        ]
        if len(safe) != len(items):
            self._issues.append("registry_unsafe_record")
        return safe

    def _append_unique(
        self,
        filename: str,
        items: Sequence[BaseModel],
        id_field: str,
    ) -> int:
        path = self.root / filename
        lock_path = self.root / f".{filename}.lock"
        written = 0
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            existing = self._existing_ids(path, id_field)
            with path.open("a", encoding="utf-8") as file:
                for item in items:
                    item_id = str(getattr(item, id_field))
                    if item_id in existing:
                        continue
                    file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
                    file.flush()
                    existing.add(item_id)
                    written += 1
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return written

    def _existing_ids(self, path: Path, id_field: str) -> set[str]:
        if not path.exists():
            return set()
        ids: set[str] = set()
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    payload: Any = json.loads(line)
                except json.JSONDecodeError:
                    self._issues.append("registry_invalid_json")
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get(id_field), str):
                    self._issues.append("registry_invalid_record")
                    continue
                ids.add(payload[id_field])
        return ids

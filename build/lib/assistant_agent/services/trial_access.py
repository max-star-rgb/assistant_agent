"""Trial-user allowlist helpers for local/pilot Web Console access."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel


TRIAL_USER_IDS_ENV = "MULTIMODAL_AGENT_TRIAL_USER_IDS"
TRIAL_USER_ID_FILE_ENV = "MULTIMODAL_AGENT_TRIAL_USER_ID_FILE"
MAX_TRIAL_USER_ID_LENGTH = 64


class TrialAccessStatus(BaseModel):
    """Public status returned to the Web Console before enabling the demo UI."""

    user_id: str
    access_required: bool
    allowed: bool
    allowed_user_count: int
    reason: str | None = None


@dataclass(frozen=True)
class TrialAccessGate:
    """Small allowlist gate.

    An empty allowlist means local demo mode remains open. Supplying one or more
    IDs turns on restricted pilot mode for the demo run endpoints.
    """

    allowed_user_ids: frozenset[str]

    @property
    def access_required(self) -> bool:
        return bool(self.allowed_user_ids)

    @property
    def allowed_user_count(self) -> int:
        return len(self.allowed_user_ids)

    def check(self, user_id: str) -> TrialAccessStatus:
        normalized = normalize_trial_user_id(user_id)
        if not normalized:
            return TrialAccessStatus(
                user_id="",
                access_required=self.access_required,
                allowed=False,
                allowed_user_count=self.allowed_user_count,
                reason="工号不能为空。",
            )
        if not self.access_required or normalized in self.allowed_user_ids:
            return TrialAccessStatus(
                user_id=normalized,
                access_required=self.access_required,
                allowed=True,
                allowed_user_count=self.allowed_user_count,
            )
        return TrialAccessStatus(
            user_id=normalized,
            access_required=True,
            allowed=False,
            allowed_user_count=self.allowed_user_count,
            reason="该工号不在服务器试用名单中。",
        )


def normalize_trial_user_id(value: object) -> str:
    """Normalize the Web Console user id consistently on client and server."""

    return re.sub(r"\s+", "_", str(value or "").strip())[:MAX_TRIAL_USER_ID_LENGTH]


def parse_trial_user_ids(value: str | None) -> list[str]:
    """Parse comma/newline/semicolon separated user ids."""

    if not value:
        return []
    ids: list[str] = []
    for raw in re.split(r"[,;\n]+", value):
        normalized = normalize_trial_user_id(raw)
        if normalized:
            ids.append(normalized)
    return ids


def trial_access_gate_from_env(
    env: Mapping[str, str] | None = None,
    *,
    base_dir: Path | None = None,
) -> TrialAccessGate:
    """Build a gate from environment variables and an optional allowlist file."""

    source = os.environ if env is None else env
    ids = set(parse_trial_user_ids(source.get(TRIAL_USER_IDS_ENV)))
    file_ref = str(source.get(TRIAL_USER_ID_FILE_ENV) or "").strip()
    if file_ref:
        ids.update(_read_trial_user_ids_file(file_ref, base_dir=base_dir))
    return TrialAccessGate(frozenset(sorted(ids)))


def _read_trial_user_ids_file(file_ref: str, *, base_dir: Path | None) -> list[str]:
    path = Path(file_ref).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if not path.exists():
        raise FileNotFoundError(f"{TRIAL_USER_ID_FILE_ENV} points to a missing file: {path}")
    ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        ids.extend(parse_trial_user_ids(line))
    return ids

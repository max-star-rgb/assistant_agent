from __future__ import annotations

import re
from pathlib import Path


INVARIANT_ID_PATTERN = re.compile(r"^[A-Z]+-[0-9]{3}$")
CORE_TEST_PATH_PATTERN = re.compile(
    r"`(tests/core/"
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.py)`"
)


def parse_invariant_registry(path: Path) -> dict[str, set[str]]:
    registered: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if len(cells) != 3:
            continue
        invariant_id, contract, responsible_files = cells
        if (
            INVARIANT_ID_PATTERN.fullmatch(invariant_id) is None
            or not contract
        ):
            continue
        paths = set(CORE_TEST_PATH_PATTERN.findall(responsible_files))
        if not paths:
            continue
        registered.setdefault(invariant_id, set()).update(paths)
    return registered

"""Strict parser for the safe unified-diff subset accepted by coding mode."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.policy import CodingPathPolicy, CodingPolicyError


FORBIDDEN_PATCH_HEADERS = (
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
    "dissimilarity index ",
    "GIT binary patch",
    "Binary files ",
)


class CodingPatchError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ParsedCodingPatchFile:
    path: str
    is_new: bool


@dataclass(frozen=True)
class ParsedCodingPatch:
    patch: str
    patch_digest: str
    files: tuple[ParsedCodingPatchFile, ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)


def parse_coding_patch(
    patch: str,
    *,
    policy: CodingPathPolicy,
    root: Path,
    limits: CodingConfig,
) -> ParsedCodingPatch:
    if not isinstance(patch, str) or not patch or "\x00" in patch or "\r" in patch:
        raise CodingPatchError("patch_invalid")
    encoded = patch.encode("utf-8")
    if len(encoded) > limits.max_patch_bytes:
        raise CodingPatchError("patch_too_large")
    lines = patch.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts or starts[0] != 0:
        raise CodingPatchError("patch_invalid")
    starts.append(len(lines))
    files: list[ParsedCodingPatchFile] = []
    seen: set[str] = set()
    for position in range(len(starts) - 1):
        section = lines[starts[position] : starts[position + 1]]
        if any(
            line.startswith(prefix)
            for line in section
            for prefix in FORBIDDEN_PATCH_HEADERS
        ):
            raise CodingPatchError("patch_operation_forbidden")
        header = section[0].split(" ")
        if len(header) != 4 or not header[2].startswith("a/") or not header[3].startswith("b/"):
            raise CodingPatchError("patch_invalid")
        old_path = header[2][2:]
        new_path = header[3][2:]
        if old_path != new_path or not new_path or any(character in new_path for character in ('"', "\\")):
            raise CodingPatchError("patch_path_mismatch")
        minus = next((line for line in section if line.startswith("--- ")), None)
        plus = next((line for line in section if line.startswith("+++ ")), None)
        if minus is None or plus is None or plus != f"+++ b/{new_path}":
            raise CodingPatchError("patch_invalid")
        is_new = minus == "--- /dev/null"
        if not is_new and minus != f"--- a/{new_path}":
            raise CodingPatchError("patch_path_mismatch")
        if is_new and "new file mode 100644" not in section:
            raise CodingPatchError("patch_invalid")
        if not any(line.startswith("@@ ") for line in section):
            raise CodingPatchError("patch_invalid")
        try:
            policy.validate_relative_path(root, new_path, operation="write")
        except CodingPolicyError as exc:
            raise CodingPatchError(exc.code) from exc
        if new_path in seen:
            raise CodingPatchError("patch_path_mismatch")
        seen.add(new_path)
        files.append(ParsedCodingPatchFile(path=new_path, is_new=is_new))
    if len(files) > limits.max_changed_files:
        raise CodingPatchError("patch_too_large")
    return ParsedCodingPatch(
        patch=patch,
        patch_digest=hashlib.sha256(encoded).hexdigest(),
        files=tuple(files),
    )


__all__ = [
    "CodingPatchError",
    "ParsedCodingPatch",
    "ParsedCodingPatchFile",
    "parse_coding_patch",
]


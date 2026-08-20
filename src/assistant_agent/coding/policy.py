"""Fail-closed path policy for governed coding workspaces."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal


DEFAULT_WRITABLE_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".go",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
DEFAULT_PROTECTED_GLOBS = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
)


class CodingPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CodingPathPolicy:
    def __init__(
        self,
        *,
        writable_suffixes: frozenset[str] = DEFAULT_WRITABLE_SUFFIXES,
        protected_globs: tuple[str, ...] = DEFAULT_PROTECTED_GLOBS,
    ) -> None:
        self.writable_suffixes = writable_suffixes
        self.protected_globs = protected_globs

    def validate_relative_path(
        self,
        root: Path,
        raw_path: str,
        *,
        operation: Literal["read", "write"],
    ) -> Path:
        normalized = str(raw_path).strip()
        relative = Path(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise CodingPolicyError("path_invalid", "coding path is invalid")
        relative_posix = relative.as_posix()
        if any(
            fnmatchcase(relative_posix, pattern)
            for pattern in self.protected_globs
        ):
            raise CodingPolicyError("path_protected", "coding path is protected")

        resolved_root = root.resolve()
        candidate = resolved_root / relative
        current = resolved_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() and operation == "write":
                raise CodingPolicyError(
                    "symlink_escape",
                    "coding writes cannot traverse symlinks",
                )
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise CodingPolicyError(
                "symlink_escape",
                "coding path escapes the workspace",
            ) from exc
        if operation == "write" and relative.suffix.lower() not in self.writable_suffixes:
            raise CodingPolicyError(
                "file_type_unsupported",
                "coding write file type is unsupported",
            )
        return candidate


__all__ = [
    "CodingPathPolicy",
    "CodingPolicyError",
    "DEFAULT_PROTECTED_GLOBS",
    "DEFAULT_WRITABLE_SUFFIXES",
]


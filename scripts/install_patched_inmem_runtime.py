#!/usr/bin/env python3
"""Build and install the repository's event-driven in-memory runtime patch."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = (
    REPO_ROOT
    / "patches"
    / "langgraph-runtime-inmem"
    / "0.32.4-event-wakeup.patch"
)
UPSTREAM_VERSION = "0.32.4"
LOCAL_VERSION = "0.32.4+assistant1"
UPSTREAM_WHEEL_SHA256 = (
    "85b9649c31c04288ebefa3c8733ae01ce1a636c9f221bf27a89f314aae908391"
)
PATCHED_FILE_SHA256 = {
    "langgraph_runtime_inmem/ops.py": (
        "254a9e2961022ca6b64fa256d28d4a743f8b8f40fd0a6eadf55fb694516a3c8a"
    ),
    "langgraph_runtime_inmem/queue.py": (
        "381bd4b53116cac48fe08cdf1889696db3db93e56550ef329e5f344731586987"
    ),
    "langgraph_runtime_inmem/queue_signal.py": (
        "f889671bb5d806c5b3ee803d22d63996063530c26185ed94e24a124703e01e18"
    ),
}


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def _single_match(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {pattern!r} in {directory}, found {len(matches)}"
        )
    return matches[0]


def _verify_upstream_wheel(wheel_path: Path) -> None:
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    if digest != UPSTREAM_WHEEL_SHA256:
        raise RuntimeError(
            "langgraph-runtime-inmem upstream wheel digest changed; "
            "review and rebase the event-wakeup patch before installing"
        )


def _set_local_version(unpacked_root: Path) -> None:
    dist_info = _single_match(unpacked_root, "*.dist-info")
    metadata_path = dist_info / "METADATA"
    metadata = metadata_path.read_text(encoding="utf-8")
    expected = f"Version: {UPSTREAM_VERSION}\n"
    if metadata.count(expected) != 1:
        raise RuntimeError("unexpected upstream wheel metadata version")
    metadata_path.write_text(
        metadata.replace(expected, f"Version: {LOCAL_VERSION}\n"),
        encoding="utf-8",
    )
    dist_info.rename(
        dist_info.with_name(f"langgraph_runtime_inmem-{LOCAL_VERSION}.dist-info")
    )


def _reject_unexpected_build_artifacts(unpacked_root: Path) -> None:
    unexpected = [
        path.relative_to(unpacked_root)
        for path in unpacked_root.rglob("*")
        if "__pycache__" in path.parts or path.suffix in {".orig", ".rej", ".pyc"}
    ]
    if unexpected:
        rendered = ", ".join(map(str, unexpected))
        raise RuntimeError(f"unexpected files in patched wheel: {rendered}")


def build_and_install() -> Path:
    with tempfile.TemporaryDirectory(prefix="assistant-agent-inmem-runtime-") as raw:
        workdir = Path(raw)
        download_dir = workdir / "download"
        unpack_dir = workdir / "unpacked"
        dist_dir = workdir / "dist"
        download_dir.mkdir()
        unpack_dir.mkdir()
        dist_dir.mkdir()

        _run(
            sys.executable,
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(download_dir),
            f"langgraph-runtime-inmem=={UPSTREAM_VERSION}",
        )
        upstream_wheel = _single_match(download_dir, "*.whl")
        _verify_upstream_wheel(upstream_wheel)
        _run(
            sys.executable,
            "-m",
            "wheel",
            "unpack",
            "--dest",
            str(unpack_dir),
            str(upstream_wheel),
        )
        unpacked_root = _single_match(unpack_dir, "langgraph_runtime_inmem-*")
        _run(
            "patch",
            "--batch",
            "--forward",
            "--no-backup-if-mismatch",
            "-p1",
            "--input",
            str(PATCH_PATH),
            cwd=unpacked_root,
        )
        _reject_unexpected_build_artifacts(unpacked_root)
        _set_local_version(unpacked_root)
        _run(
            sys.executable,
            "-m",
            "wheel",
            "pack",
            "--dest-dir",
            str(dist_dir),
            str(unpacked_root),
        )
        patched_wheel = _single_match(dist_dir, "*.whl")
        _run(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(patched_wheel),
        )
        installed_name = patched_wheel.name

    return Path(installed_name)


def check_installation() -> bool:
    try:
        distribution = importlib.metadata.distribution("langgraph-runtime-inmem")
    except importlib.metadata.PackageNotFoundError:
        return False
    if distribution.version != LOCAL_VERSION:
        return False
    package_root = Path(distribution.locate_file(""))
    for relative_path, expected_digest in PATCHED_FILE_SHA256.items():
        path = package_root / relative_path
        if not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the event-driven LangGraph in-memory runtime fork."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the patched runtime without downloading or installing anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        installed = check_installation()
        print("patched in-memory runtime: " + ("ready" if installed else "missing"))
        return 0 if installed else 1
    installed_wheel = build_and_install()
    print(f"installed {installed_wheel.name}")
    print("restart the existing langgraph dev process for the patch to take effect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

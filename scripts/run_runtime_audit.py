#!/usr/bin/env python3
"""Stable entrypoint for the read-only Langfuse-first runtime audit."""

from assistant_agent.observability.runtime_audit.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

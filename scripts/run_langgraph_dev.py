#!/usr/bin/env python3
"""Run LangGraph dev without watching its own persistence directory."""

from __future__ import annotations

import sys

from uvicorn.supervisors.watchfilesreload import WatchFilesReload
from watchfiles import DefaultFilter, watch


def build_watch_filter() -> DefaultFilter:
    """Ignore LangGraph's runtime persistence before WatchFiles logs events."""

    return DefaultFilter(
        ignore_dirs=(*DefaultFilter.ignore_dirs, ".langgraph_api"),
    )


def install_watch_filter() -> None:
    """Apply the upstream workaround before Uvicorn creates its reloader."""

    original_init = WatchFilesReload.__init__
    watch_filter = build_watch_filter()

    def filtered_init(self, config, target, sockets) -> None:
        original_init(self, config, target, sockets)
        self.watcher = watch(
            *self.reload_dirs,
            watch_filter=watch_filter,
            stop_event=self.should_exit,
            yield_on_timeout=True,
            ignore_permission_denied=True,
        )

    WatchFilesReload.__init__ = filtered_init


def main() -> None:
    install_watch_filter()
    sys.argv.insert(1, "dev")
    from langgraph_cli.cli import cli

    cli()


if __name__ == "__main__":
    main()

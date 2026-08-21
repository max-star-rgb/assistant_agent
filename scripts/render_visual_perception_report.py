#!/usr/bin/env python3
"""Render a prompt-safe local HTML timeline from Agent Server logs."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from time import monotonic, sleep
from urllib.parse import parse_qs, urlparse
import webbrowser


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.observability.visual_perception_report import (  # noqa: E402
    parse_visual_perception_log,
    render_visual_perception_html,
)
from assistant_agent.observability.visual_perception_live import (  # noqa: E402
    VisualPerceptionLiveFeed,
    format_visual_perception_sse,
)


LIVE_HOST = "127.0.0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--session-digest")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.live:
        if args.output is not None:
            raise SystemExit("--output cannot be combined with --live")
        if not 0 <= args.port <= 65_535:
            raise SystemExit("--port must be within [0, 65535]")
        return _serve_live(
            log_file=args.log_file,
            session_digest=args.session_digest,
            port=args.port,
            open_browser=args.open_browser,
        )
    if args.session_digest is None:
        raise SystemExit("--session-digest is required unless --live is used")
    try:
        with args.log_file.open("r", encoding="utf-8", errors="replace") as source:
            report = parse_visual_perception_log(
                source,
                session_digest=args.session_digest,
            )
    except OSError as exc:
        raise SystemExit(f"cannot read log file: {exc}") from exc
    output = args.output or (
        REPO_ROOT
        / ".data"
        / "diagnostics"
        / f"visual-perception-{report.session_digest}.html"
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_visual_perception_html(report), encoding="utf-8")
    print(output)
    if args.open_browser:
        webbrowser.open(output.as_uri())
    return 0


def _serve_live(
    *,
    log_file: Path,
    session_digest: str | None,
    port: int,
    open_browser: bool,
) -> int:
    feed = VisualPerceptionLiveFeed(log_file, session_digest=session_digest)
    handler = _handler_for(feed)
    server = ThreadingHTTPServer((LIVE_HOST, port), handler)
    server.daemon_threads = True
    url = f"http://{LIVE_HOST}:{server.server_port}/"
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _handler_for(feed: VisualPerceptionLiveFeed) -> type[BaseHTTPRequestHandler]:
    class VisualPerceptionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_report()
                return
            if parsed.path == "/events":
                self._serve_events(parsed.query)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _serve_report(self) -> None:
            body = render_visual_perception_html(
                feed.snapshot(),
                live_events_url="/events",
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _serve_events(self, query: str) -> None:
            cursor = _event_cursor(self.headers.get("Last-Event-ID"), query)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_keepalive = monotonic()
            try:
                while True:
                    events = feed.events_after(cursor)
                    for event in events:
                        cursor = event["order"]
                        self.wfile.write(
                            format_visual_perception_sse(event, event_id=cursor)
                        )
                    now = monotonic()
                    if events or now - last_keepalive >= 10.0:
                        if not events:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_keepalive = now
                    sleep(0.25)
            except OSError:
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return VisualPerceptionHandler


def _event_cursor(last_event_id: str | None, query: str) -> int:
    candidates = [value for value in (last_event_id,) if value]
    candidates.extend(parse_qs(query).get("after", ()))
    parsed = []
    for candidate in candidates:
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            parsed.append(value)
    return max(parsed, default=0)


if __name__ == "__main__":
    raise SystemExit(main())

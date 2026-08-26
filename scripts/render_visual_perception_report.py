#!/usr/bin/env python3
"""Render a prompt-safe local HTML timeline from Agent Server logs."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import stat
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
DEFAULT_KEYFRAME_ROOT = REPO_ROOT / ".data" / "visual_perception" / "keyframes"
_KEYFRAME_ROUTE = re.compile(
    r"^/keyframes/(?P<session_digest>[0-9a-f]{16})/(?P<sequence>[1-9][0-9]*)\.jpg$"
)
_SESSION_DIRECTORY = re.compile(r"^agent-service-video-[0-9a-f]{24}$")
_KEYFRAME_FILE = re.compile(r"^frame-[0-9]{8}-[0-9a-f]{32}\.jpg$")
_MAX_KEYFRAME_BYTES = 8 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--session-digest")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--keyframe-root", type=Path, default=DEFAULT_KEYFRAME_ROOT)
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
            keyframe_root=args.keyframe_root,
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
    keyframe_root: Path,
    open_browser: bool,
) -> int:
    feed = VisualPerceptionLiveFeed(log_file, session_digest=session_digest)
    handler = _handler_for(feed, keyframe_root=keyframe_root)
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


def _handler_for(
    feed: VisualPerceptionLiveFeed,
    *,
    keyframe_root: Path,
) -> type[BaseHTTPRequestHandler]:
    configured_keyframe_root = keyframe_root.expanduser().absolute()

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
            if _KEYFRAME_ROUTE.fullmatch(parsed.path) is not None:
                self._serve_keyframe(parsed.path)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _serve_report(self) -> None:
            body = render_visual_perception_html(
                feed.snapshot(),
                live_events_url="/events",
                live_keyframes_url="/keyframes",
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _serve_keyframe(self, path: str) -> None:
            match = _KEYFRAME_ROUTE.fullmatch(path)
            if match is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            session_digest = match.group("session_digest")
            sequence = int(match.group("sequence"))
            snapshot = feed.snapshot()
            if snapshot.session_digest != session_digest or not any(
                event.get("event_name") == "semantic_frame.selected"
                and event.get("session_id_digest") == session_digest
                and event.get("sequence") == sequence
                for event in snapshot.events
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = _read_keyframe(
                configured_keyframe_root,
                session_digest=session_digest,
                sequence=sequence,
            )
            if body is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
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


def _read_keyframe(
    keyframe_root: Path,
    *,
    session_digest: str,
    sequence: int,
) -> bytes | None:
    if not session_digest or sequence <= 0:
        return None
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    open_fds: list[int] = []
    try:
        root_fd = os.open(keyframe_root, directory_flags)
        open_fds.append(root_fd)
        semantic_input_fd = os.open(
            "semantic-input",
            directory_flags,
            dir_fd=root_fd,
        )
        open_fds.append(semantic_input_fd)
        session_prefix = f"agent-service-video-{session_digest}"
        session_names = tuple(
            name
            for name in os.listdir(semantic_input_fd)
            if name.startswith(session_prefix)
            and _SESSION_DIRECTORY.fullmatch(name) is not None
        )
        if len(session_names) != 1:
            return None
        session_fd = os.open(
            session_names[0],
            directory_flags,
            dir_fd=semantic_input_fd,
        )
        open_fds.append(session_fd)
        frame_prefix = f"frame-{sequence:08d}-"
        frame_names = tuple(
            name
            for name in os.listdir(session_fd)
            if name.startswith(frame_prefix)
            and _KEYFRAME_FILE.fullmatch(name) is not None
        )
        if len(frame_names) != 1:
            return None
        frame_fd = os.open(frame_names[0], file_flags, dir_fd=session_fd)
        open_fds.append(frame_fd)
        frame_stat = os.fstat(frame_fd)
        if (
            not stat.S_ISREG(frame_stat.st_mode)
            or frame_stat.st_size <= 0
            or frame_stat.st_size > _MAX_KEYFRAME_BYTES
        ):
            return None
        with os.fdopen(os.dup(frame_fd), "rb") as stream:
            body = stream.read(_MAX_KEYFRAME_BYTES + 1)
        if len(body) != frame_stat.st_size:
            return None
        return body
    except OSError:
        return None
    finally:
        for file_descriptor in reversed(open_fds):
            try:
                os.close(file_descriptor)
            except OSError:
                pass


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

#!/usr/bin/env python3
"""PyCharm-owned entrypoints for the backend and realtime frontend."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
UNITS = {"backend": "assistant-agent-v2.service", "frontend": "assistant-qwen-gateway.service"}


def load_environment(role, root, inherited):
    env = dict(inherited)
    files = [root / ".env", root / "local/deployment.env"] if role == "backend" else [
        root / "integrations/qwen_audio_agent/.env"]
    for path in files:
        if not path.is_file():
            raise SystemExit(f"Missing local environment file: {path}")
        env.update({key: value for key, value in dotenv_values(path).items() if value is not None})
    if role == "frontend":
        env.update(ASSISTANT_AGENT_URL="http://127.0.0.1:8088", QWEN_AUDIO_PORT="3101", QWEN_AUDIO_MEDIA_PORT="8081")
    return env


def find_node():
    candidates = [Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"]
    candidates += sorted((Path.home() / ".nvm/versions/node").glob("*/bin/node"), reverse=True)
    if found := shutil.which("node"):
        candidates.append(Path(found))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True)
        if result.returncode:
            continue
        version = tuple(int(part) for part in result.stdout.strip().lstrip("v").split("."))
        major = version[0]
        if (major == 22 and version >= (22, 22, 2)) or (major == 24 and version >= (24, 15, 0)) or major >= 26:
            return str(candidate)
    raise SystemExit("Node.js 22.22.2 / 24.15.0 or newer supported release is required")


def command_for(role, root, env):
    if role == "backend":
        return [sys.executable, str(root / "scripts/run_server.py"), "--host", "0.0.0.0",
                "--port", "8088", "--no-env-file"], root
    return [find_node(), "src/gateway.mjs"], root / "integrations/qwen_audio_agent"


def stop_managed_service(role, root):
    result = subprocess.run(["systemctl", "--user", "show", UNITS[role],
                             "--property=WorkingDirectory", "--value"], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        return
    expected = root if role == "backend" else root / "integrations/qwen_audio_agent"
    if Path(result.stdout.strip()).resolve() != expected.resolve():
        raise SystemExit(f"Refusing to stop {UNITS[role]}: working directory differs")
    subprocess.run(["systemctl", "--user", "stop", UNITS[role]], check=True)


def find_frontend_launchers(root, *, roles=("frontend",), proc_root=Path("/proc")):
    """Match this project's launchers for the requested roles."""
    script = (root / "scripts/run_pycharm.py").resolve()
    matches = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = (entry / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            if len(args) < 3 or os.fsdecode(args[2]) not in roles:
                continue
            candidate = Path(os.fsdecode(args[1]))
            if not candidate.is_absolute():
                candidate = (entry / "cwd").resolve() / candidate
            if candidate.resolve() == script:
                matches.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return sorted(matches)


def stop_previous_frontend(root, *, roles=("frontend",)):
    for pid in find_frontend_launchers(root, roles=roles):
        try:
            if pid not in find_frontend_launchers(root, roles=roles):
                continue
            print(f"Stopping previous launcher {pid} for this project...", flush=True)
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 20
            while pid in find_frontend_launchers(root, roles=roles):
                if time.monotonic() >= deadline:
                    raise SystemExit("Previous frontend did not stop; inspect its console before retrying")
                time.sleep(0.1)
        except ProcessLookupError:
            pass


def clear_ports(ports):
    """Terminate TCP listeners, escalating only if they still own these ports."""
    if not shutil.which("lsof"):
        raise SystemExit("Port cleanup requires lsof; install it before running this configuration")

    def listeners():
        result = subprocess.run(
            ["lsof", "-w", "-nP", "-t", "-a", "-iTCP:" + ",".join(map(str, ports)), "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise SystemExit(f"Cannot inspect listening ports: {result.stderr.strip()}")
        return {int(pid) for pid in result.stdout.split()}

    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = listeners()
        if not pids:
            return
        if os.getpid() in pids:
            raise SystemExit("Refusing to terminate the current launcher")
        for pid in pids:
            print(f"Releasing ports {ports}: {sig.name} to PID {pid}", flush=True)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise SystemExit(f"No permission to stop PID {pid} on ports {ports}") from None
        deadline = time.monotonic() + 5
        while listeners():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        else:
            return
    raise SystemExit(f"Ports {ports} are still occupied after cleanup")


def wait_backend(process=None, timeout=90, identity="local-developer"):
    opener = build_opener(ProxyHandler({}))
    request = Request("http://127.0.0.1:8088/qwen-audio/capabilities",
                      headers={"X-Assistant-User": identity})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise SystemExit("Backend exited before becoming ready; see its console output")
        try:
            with opener.open(request, timeout=2) as response:
                capabilities = json.load(response)
                if isinstance(capabilities, dict) and "search_tool" in capabilities:
                    tool = capabilities["search_tool"]
                    if tool is None or (
                        isinstance(tool, dict)
                        and tool.get("type") == "function"
                        and isinstance(tool.get("function"), dict)
                        and tool["function"].get("name") == "mcp_bailian_bailian_web_search"
                    ):
                        return
                raise SystemExit("Backend returned invalid frontend capabilities")
        except HTTPError as exc:
            if exc.code < 500:
                raise SystemExit(f"Backend capabilities HTTP {exc.code}; check backend routes and ASSISTANT_AGENT_USER") from None
        except (URLError, OSError):
            pass
        time.sleep(0.5)
    raise SystemExit("Backend capabilities on 8088 are not ready; check backend Graph initialization logs")


def stop_process(process):
    # Each launcher has its own process group, including LangGraph reload children.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["backend", "frontend", "all"])
    parser.add_argument("--clear-ports", action="store_true", help="Compatibility flag; cleanup is always enabled")
    args = parser.parse_args()
    roles = ["backend", "frontend"] if args.role == "all" else [args.role]
    prepared = []
    for role in roles:
        env = load_environment(role, ROOT, os.environ)
        command, cwd = command_for(role, ROOT, env)
        prepared.append((role, command, cwd, env))
    for role in reversed(roles):
        stop_managed_service(role, ROOT)
    stop_previous_frontend(ROOT, roles=(*roles, "all"))
    ports = [port for role in roles for port in ({"backend": [8088], "frontend": [3101, 8081]}[role])]
    clear_ports(ports)
    processes = []

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        for role, command, cwd, env in prepared:
            if role == "frontend":
                print("Waiting for backend Graph and frontend capabilities on 8088...", flush=True)
                wait_backend(processes[0] if processes else None,
                             identity=env.get("ASSISTANT_AGENT_USER") or "local-developer")
            print(f"Starting {role} under PyCharm", flush=True)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True))
        while all(process.poll() is None for process in processes):
            time.sleep(0.2)
        return next(process.returncode for process in processes if process.returncode is not None)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for process in reversed(processes):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())

"""Client-owned, pinned Playwright browser runtime and MCP sessions."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, ConfigDict, Field

MCP_VERSION = "0.0.78"
BROWSER_TOOLS = frozenset((
    "browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
    "browser_evaluate", "browser_file_upload", "browser_drop", "browser_find", "browser_fill_form",
    "browser_press_key", "browser_type", "browser_navigate", "browser_navigate_back",
    "browser_network_requests", "browser_network_request", "browser_run_code_unsafe",
    "browser_take_screenshot", "browser_snapshot", "browser_click", "browser_drag",
    "browser_hover", "browser_select_option", "browser_tabs", "browser_wait_for",
))


def install_browser(cwd: Path):
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("浏览器安装需要 Node.js 20+（含 npm），请先安装 Node.js")
    if int(subprocess.check_output([node, "-p", "process.versions.node.split('.')[0]"], text=True).strip()) < 20:
        raise RuntimeError("浏览器安装需要 Node.js 20+，请更新 PATH 中的 Node.js")
    root = cwd / ".agent/browser-runtime"
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers"), "npm_config_cache": str(root / "npm-cache")}
    subprocess.run([npm, "install", "--prefix", str(root), "--no-audit", "--no-fund", f"@playwright/mcp@{MCP_VERSION}"], env=env, check=True)
    subprocess.run([node, str(root / "node_modules/playwright/cli.js"), "install", "chromium"], env=env, check=True)
    executable = subprocess.check_output([node, "-e", "process.stdout.write(require('playwright').chromium.executablePath())"], cwd=root, env=env, text=True)
    (root / "runtime.json").write_text(json.dumps({"node": node, "executable": executable, "version": MCP_VERSION}))


def browser_connection(cwd: Path, thread_id: str, *, headless: bool):
    root = cwd / ".agent/browser-runtime"
    manifest = root / "runtime.json"
    if not manifest.is_file():
        raise ValueError("browser_not_installed: run assistant-agent --install-browser --cwd <cwd>")
    runtime = json.loads(manifest.read_text())
    if runtime["version"] != MCP_VERSION or not Path(runtime["executable"]).is_file():
        raise ValueError("browser_runtime_mismatch: reinstall browser")
    scope = hashlib.sha256(thread_id.encode()).hexdigest()[:24]
    output = cwd / ".agent/browser" / scope
    output.mkdir(parents=True, exist_ok=True)
    args = [str(root / "node_modules/@playwright/mcp/cli.js"),
            "--executable-path", runtime["executable"], "--user-data-dir", str(output / "profile"),
            "--output-dir", str(output)]
    if headless:
        args.append("--headless")
    return {"transport": "stdio", "command": runtime["node"], "args": args, "cwd": str(cwd),
            "env": {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()),
                    "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers"),
                    **{key: os.environ[key] for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR") if key in os.environ}}}


class BrowserCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    thread_id: str = Field(min_length=1, max_length=160)
    name: str
    arguments: dict


class DeviceBrowser:
    def __init__(self, cwd: Path, *, headless=False):
        self.cwd, self.headless = cwd, headless
        self.sessions = {}

    async def _worker(self, thread_id, queue):
        try:
            connection = await asyncio.to_thread(browser_connection, self.cwd, thread_id, headless=self.headless)
            client = MultiServerMCPClient({"playwright": connection})
            async with client.session("playwright") as session:
                while True:
                    call, future = await queue.get()
                    if call is None:
                        return
                    try:
                        result = await session.call_tool(call.name, call.arguments)
                        if not future.done():
                            future.set_result(result.model_dump(mode="json"))
                    except Exception:
                        if not future.done():
                            future.set_exception(ValueError("browser_call_failed"))
                        raise
        finally:
            while not queue.empty():
                _, future = queue.get_nowait()
                if future is not None and not future.done():
                    future.set_exception(ValueError("browser_session_closed"))

    async def call(self, **arguments):
        call = BrowserCall(**arguments)
        if call.name not in BROWSER_TOOLS:
            raise ValueError("browser_tool_not_allowed")
        entry = self.sessions.get(call.thread_id)
        if entry is None:
            # ponytail: eight live thread browsers per connection; add idle eviction if needed.
            if len(self.sessions) >= 8:
                raise ValueError("too many browser sessions")
            queue = asyncio.Queue()
            task = asyncio.create_task(self._worker(call.thread_id, queue))
            entry = self.sessions[call.thread_id] = queue, task
        queue, task = entry
        future = asyncio.get_running_loop().create_future()
        await queue.put((call, future))
        try:
            done, _ = await asyncio.wait({future, task}, timeout=120, return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            if task in done:
                await task
            raise ValueError("browser_unavailable_or_timeout")
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.sessions.pop(call.thread_id, None)
            future.cancel()
            raise

    async def close(self):
        for queue, _ in self.sessions.values():
            await queue.put((None, None))
        await asyncio.gather(*(task for _, task in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()

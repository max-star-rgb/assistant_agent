"""Installed same-host client: SDK conversation plus local execution, no Agent loop."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import termios
from uuid import uuid4
from urllib.parse import urlsplit

import httpx
from langgraph_sdk import get_client

from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.native_agent.context import AssistantRunContext, ensure_working_directory
from assistant_agent.runtime.device_client import run_device_client


def _enable_utf8_erase():
    encoding = sys.stdin.encoding or "utf-8"
    if sys.platform == "linux" and sys.stdin.isatty() and encoding.lower().replace("-", "") == "utf8":
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        settings = termios.tcgetattr(fd)
        # Linux IUTF8: canonical erase must remove a character, not one byte.
        settings[0] |= 0x4000
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        return original
    return None


async def _input(prompt: str) -> str:
    """Cancelable terminal input without an uninterruptible executor thread."""
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    encoding = sys.stdin.encoding or "utf-8"
    original = _enable_utf8_erase()
    try:
        while True:
            print(prompt, end="", flush=True)
            future = loop.create_future()
            data = bytearray()
            def read():
                if future.done():
                    return
                try:
                    # No TextIO read-ahead: a pasted next line must remain fd-readable.
                    chunk = os.read(fd, 1)
                    if not chunk or chunk == b"\n":
                        future.set_result(bytes(data))
                    else:
                        data.extend(chunk)
                except OSError as exc:
                    future.set_exception(exc)
            loop.add_reader(fd, read)
            try:
                raw = await future
            finally:
                loop.remove_reader(fd)
            try:
                return raw.decode(encoding).strip()
            except UnicodeDecodeError:
                print("输入编码无效，本行未发送，请重新输入。")
    finally:
        if original is not None:
            termios.tcsetattr(fd, termios.TCSANOW, original)


async def _approval_command(interrupts):
    resume = {}
    for interrupt in interrupts:
        actions = interrupt["value"].get("action_requests", [])
        if not actions:
            raise ValueError("收到非工具审批中断，请在 Studio 中继续该会话")
        decisions = []
        for action in actions:
            print(json.dumps(action, ensure_ascii=False, indent=2))
            answer = await _input("允许执行？[y/N] ")
            decisions.append({"type": "approve" if answer.lower() == "y" else "reject"})
        resume[interrupt["id"]] = {"decisions": decisions}
    return {"resume": resume}


async def _run(args):
    address = urlsplit(args.server)
    if address.scheme not in {"http", "https"} or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("当前安装体验只接受本机 Agent Server")
    cwd = ensure_working_directory(args.cwd.expanduser().absolute())
    headers = {"X-Assistant-User": args.identity}
    device = f"desktop-{uuid4().hex}"
    async with httpx.AsyncClient(base_url=args.server, headers=headers, trust_env=False) as http:
        response = await http.post("/local-devices", json={"device_id": device, "cwd": str(cwd)})
        response.raise_for_status()
        token = response.json()["token"]
        ready = asyncio.Event()
        url = args.server.replace("http://", "ws://", 1).replace("https://", "wss://", 1).rstrip("/")
        executor = asyncio.create_task(run_device_client(
            f"{url}/local-devices/connect", token, cwd,
            allow_shell=args.allow_shell, host_paths=True, ready=ready,
            browser_headless=args.browser_headless,
        ))
        client = get_client(url=args.server, headers=headers)
        context = AssistantRunContext(cwd=cwd, device_id=device, require_tool_approval=True)
        state_file = cwd / ".agent" / ("client-" + hashlib.sha256(
            f"{args.server}\0{args.identity}".encode()).hexdigest()[:16] + ".json")

        async def conversation():
            await asyncio.wait_for(ready.wait(), 10)
            print(f"已连接 {args.server}；帐号 {args.identity}；执行目录 {cwd}")
            if args.check:
                print("本地执行组件连接成功；未调用模型。")
                return
            saved = json.loads(state_file.read_text()) if state_file.exists() else {}
            thread_id = args.thread_id or saved.get("thread_id")
            if thread_id:
                thread = await client.threads.get(thread_id)
                if thread.get("metadata", {}).get("graph_id") != ASSISTANT_GRAPH_ID:
                    raise ValueError("旧会话图版本不兼容，请使用新的工作目录或指定新会话")
            else:
                thread = await client.threads.create(graph_id=ASSISTANT_GRAPH_ID)
                thread_id = thread["thread_id"]
                state_file.write_text(json.dumps({"thread_id": thread_id}))
            print(f"会话 {thread_id}；输入 /exit 退出。")
            pending = [item for group in thread.get("interrupts", {}).values() for item in group]
            text = " ".join(args.text).strip()
            while True:
                resuming = bool(pending)
                command = await _approval_command(pending) if pending else None
                pending = []
                if command is None:
                    text = text or await _input("你> ")
                    if not text or text == "/exit":
                        return
                while True:
                    created = {}
                    try:
                        result = await client.runs.wait(
                            thread_id, ASSISTANT_GRAPH_ID,
                            input=None if command else {"messages": [{"role": "user", "content": text}]},
                            command=command, context=context.model_dump(mode="json"),
                            multitask_strategy="interrupt", on_disconnect="cancel",
                            on_run_created=lambda run: created.update(run),
                        )
                    except asyncio.CancelledError:
                        if created.get("run_id"):
                            await client.runs.cancel(thread_id, created["run_id"], action="interrupt", wait=True)
                        raise
                    interrupts = result.get("__interrupt__", [])
                    if not interrupts:
                        for message in reversed(result.get("messages", [])):
                            if message.get("type") == "ai" or message.get("role") == "assistant":
                                print(message.get("content", ""))
                                break
                        break
                    command = await _approval_command(interrupts)
                if resuming:
                    continue
                if args.text:
                    return
                text = ""

        session = asyncio.create_task(conversation())
        try:
            done, _ = await asyncio.wait({session, executor}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            if executor in done and not session.done():
                raise ConnectionError("本地执行连接已断开，请重新启动客户端")
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)
            # Let the server observe the closed connection before retiring the grant.
            await http.delete(f"/local-devices/{device}")


def main():
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
    parser = argparse.ArgumentParser(description="Assistant 本机安装客户端（连接现有 Agent Server）")
    parser.add_argument("text", nargs="*")
    parser.add_argument("--server", default="http://127.0.0.1:8089")
    parser.add_argument("--identity", default="local-cli")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--thread-id")
    parser.add_argument("--allow-shell", action="store_true", help="允许当前系统用户权限的 Shell；仍须逐次审批")
    parser.add_argument("--check", action="store_true", help="仅检查连接，不调用模型")
    parser.add_argument("--install-browser", action="store_true", help="安装客户端专用 Chromium 和 Playwright MCP 后退出")
    parser.add_argument("--browser-headless", action="store_true", help="在无图形环境运行产品浏览器")
    args = parser.parse_args()
    if args.install_browser:
        from assistant_agent.mcp.browser_runtime import install_browser
        install_browser(ensure_working_directory(args.cwd.expanduser().absolute()))
        return
    # Keep character-aware erase enabled while replies are running, too.
    original = _enable_utf8_erase()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    finally:
        if original is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)


if __name__ == "__main__":
    main()

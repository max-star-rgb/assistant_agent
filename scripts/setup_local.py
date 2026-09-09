#!/usr/bin/env python3
"""显式数据迁移和安装验收；不读取密钥或调用真实 Provider。"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODEL_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
MODEL_DIR = ROOT / "data/models/siglip2-base-patch16-224"
DATA_DIRS = ("agent_server", "models", "generated")


def migrate_data(root: Path) -> None:
    """停止服务后同盘重命名完整目录，包括 SQLite WAL；不合并、不覆盖。"""
    lock = root / "data/run/agent_server-dev.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("请先停止 Agent Server") from None
        moves = [(root / "local" / name, root / "data" / name)
                 for name in DATA_DIRS if (root / "local" / name).exists()]
        for source, target in moves:
            if source.is_symlink() or not source.is_dir():
                raise RuntimeError(f"拒绝迁移非普通目录：{source}")
            if target.exists():
                raise FileExistsError(f"目标已存在，拒绝合并：{target}")
        for source, target in moves:
            source.rename(target)
    print("数据目录检查完成。若 .env 显式使用旧路径，请改为 data；原配置未改写。")


def check_mock() -> None:
    # 隔离真实凭据、MCP 和用户数据。该命令只编译原生图，不启动第二个 Server。
    keep = {k: v for k, v in os.environ.items() if k in
            {"PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE"}}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update(MULTIMODAL_AGENT_PROVIDER_MODE="mock", MEMORY_BACKEND="disabled",
                      MULTIMODAL_AGENT_MCP_ENABLED="0", LANGSMITH_TRACING="false",
                      HF_HUB_OFFLINE="1")
    from assistant_agent.config import load_app_config
    from assistant_agent.mcp.config import load_mcp_server_configs_from_env
    from dotenv import dotenv_values

    config = load_app_config(dotenv_values(ROOT / ".env.example"))
    if config.provider_mode != "mock":
        raise RuntimeError("mock 配置失败")
    load_mcp_server_configs_from_env(
        {"MULTIMODAL_AGENT_MCP_ENABLED": "1"},
        config_path=ROOT / "config/mcp_servers.example.json",
    )
    load_mcp_server_configs_from_env(
        {"MULTIMODAL_AGENT_MCP_ENABLED": "1"},
        config_path=ROOT / "config/mcp_services.example.json",
    )

    async def compose():
        from langgraph.store.memory import InMemoryStore
        from assistant_agent.agent_server.services import AgentServerExecutionOwner
        owner = await AgentServerExecutionOwner.compose(store=InMemoryStore())
        try:
            for graph in (owner.graph, owner.worker_graph, owner.memory_graph):
                if not graph.get_graph().nodes:
                    raise RuntimeError("原生图为空")
        finally:
            await owner.aclose()

    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="assistant-install-check-") as directory:
        try:
            os.chdir(directory)
            # 装配包含过期资源回收；验收绝不能触碰用户 Home 下的真实 thread。
            from unittest.mock import patch
            with patch.object(Path, "home", return_value=Path(directory)):
                asyncio.run(compose())
        finally:
            os.chdir(old_cwd)
    print("PASS: mock 配置、MCP 模板和三张原生图装配；未启动 Server，未调用真实 Provider")


def check_gpu(model_dir: Path) -> None:
    from PIL import Image
    from assistant_agent.media.embedding.local_siglip2 import (
        LocalSiglip2EmbeddingConfig, LocalSiglip2EmbeddingProvider,
        load_siglip2_embedding_manifest,
    )
    from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation, TextObservation
    manifest = load_siglip2_embedding_manifest(model_dir)
    if manifest.model_revision != MODEL_REVISION or manifest.image is None or manifest.text is None:
        raise RuntimeError("需要固定 revision 的图文联合 SigLIP2 manifest")
    provider = LocalSiglip2EmbeddingProvider(LocalSiglip2EmbeddingConfig(model_dir=model_dir))
    with tempfile.TemporaryDirectory(prefix="siglip2-check-") as directory:
        path = Path(directory) / "synthetic.png"
        Image.new("RGB", (224, 224), "red").save(path)
        results = [provider.embed_image(ImageObservation(session_id="install", observation_id="image", image_ref=str(path))),
                   provider.embed_text(TextObservation(session_id="install", observation_id="text", text="a red square", source="install"))]
    for result in results:
        if not isinstance(result, EmbeddingEvent):
            raise RuntimeError(f"CUDA 图文推理失败：{result.model_dump(exclude_none=True)}")
    print("PASS: SigLIP2 checksum、CUDA-only image/text 实际推理；无云端调用")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate-data", "check", "gpu"))
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    if args.action == "migrate-data":
        migrate_data(ROOT)
    elif args.action == "check":
        check_mock()
    else:
        check_gpu(args.model_dir.resolve())


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

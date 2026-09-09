# 脚本入口

新电脑完整步骤见 [WSL 安装与迁移](../docs/wsl-installation.md)。统一命令：

```bash
bash scripts/setup_wsl.sh
bash scripts/start.sh check
bash scripts/start.sh server
# 另一个终端
bash scripts/start.sh client --check
bash scripts/start.sh client
```

`setup_wsl.sh` 配置 `hello_agent`、Node、ffmpeg、浏览器和 SigLIP2；默认不安装 BGE。
`--skip-model` 只跳过模型准备和 GPU 验收，不表示视觉可用。模板随 Git 分发，仅在私有配置不存在时复制。
`start.sh` 使用安装记录的 Python 路径，不要求每个终端手工激活 conda；服务保持 8089 回环地址。
`start.sh memory` 使用 `.env` 启动 Mem0/Qdrant，需先填写真实配置；安装阶段只构建镜像。

## 服务与客户端

- `run_server.py`：官方 `langgraph dev` 薄入口，保留单实例锁、端口检查、配置来源和仓库外日志。
  PyCharm 可继续使用 `hello_agent` 执行；默认重载，`--no-reload` 关闭。`--no-env-file` 忽略真实 `.env`。
  状态由官方 `.langgraph_api/` 保存，不启动自定义 Runtime 或 PostgreSQL。
- `agent_cli.py`：原生 SDK 对话客户端，支持 history/replay/rollback；状态变更遵守入口自身确认规则。
- `media_simulator.py`：连接既有 `/agent-service/v1` 的媒体协议模拟客户端；不会另启 Server。
- `setup_local.py init`：初始化私有配置；拒绝旧数据目录或旧 `.env` 数据路径，不覆盖文件。
- `setup_local.py migrate-data`：停止服务后把 `.local` 的模型、generated 和 agent_server 目录移到 `.data`；
  不合并目标、不删除历史数据、不改写 `.env`，迁移后按提示更新显式路径。
- `setup_local.py check`：隔离用户数据的 mock 三图装配与模板检查；不读密钥，不调用云端模型。
- `setup_local.py gpu`：SigLIP2 manifest/checksum 与实际 CUDA 图文推理；使用合成输入、不上传数据。

发行命令 `assistant-agent` 与服务在同一 Ubuntu 内使用，默认逐次工具审批；`--allow-shell` 显式允许当前
OS 用户权限的 Shell。客户端 `--install-browser --cwd 路径` 安装独立 Chromium，Node 由 WSL 安装脚本提供。
默认工作目录 `~/assistant_cqy/studio`；改 cwd 时先为那个目录安装浏览器。

## Memory 与模型准备

- `run_mem0.py`：现有 Mem0/Qdrant operator 控制台；包含真实记忆读写和删除操作，按其自身确认规则使用。
- `run_qdrant.py`：现有 Qdrant supervisor。BGE 暂不安装，不代表 Mem0 的 Qdrant 可以省略。
- `migrate_mem0_memories_to_chinese.py`：显式真实 Provider 的记忆翻译迁移；不是新电脑安装步骤，不自动执行。
- `export_siglip2_embedding_onnx.py`：下载固定 revision 后导出图文联合 ONNX，已存在目标拒绝覆盖。
  安装脚本自动调用，固定 revision 为 `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`，
  目标 `.data/models/siglip2-base-patch16-224`。
  `torch`、`transformers`、`onnxscript` 是导出依赖；运行只使用 ONNX Runtime GPU。
  `export_siglip2_vision_onnx.py` 为既有 deprecated 兼容入口，不用于新安装。

## 文档校验与已删除入口

`python scripts/check_documentation_authority.py --repo-root .` 检查 authority 路由。
原 `evals/` 已清理，依赖它的四个 `run_system_*` 包装脚本同时删除；不恢复旧 runner 或测试目录。
开发验证按 [tests/README.md](../tests/README.md) 在临时 feature 目录显式运行。

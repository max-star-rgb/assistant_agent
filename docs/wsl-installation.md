# Windows / WSL2 安装与迁移

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 新电脑安装、配置文件和本地数据迁移操作入口 |
| Owns | WSL 安装脚本、依赖约束、模板复制、模型准备、目录归属和安装验收 |
| Does not own | Graph/Tool/Memory 业务语义、视觉算法或公网生产部署 |
| 源码与 schema 入口 | `scripts/setup_wsl.sh`、`scripts/setup_local.py`、`scripts/start.sh`、`config/` |
| 验证入口 | `python scripts/setup_local.py check`、`bash -n scripts/setup_wsl.sh scripts/start.sh` |
| 相邻 authority | `agent-server-architecture.md`、`visual-perception-architecture.md`、`tool-calling-architecture.md` |

## 推荐部署位置

Windows 保留 NVIDIA 驱动、WSL2 和 Docker Desktop；Agent、Python、Node、ffmpeg、浏览器和模型都在
**WSL2 Ubuntu 24.04 x86_64** 内运行。项目放在 `~/projects/assistant_agent`，不放 `/mnt/c` 或 `/mnt/d`。
这是迁移现有受信单机服务，继续使用官方 `langgraph dev`，不代表公网、多租户或无人值守生产部署。
客户端也在同一个 Ubuntu 中运行；不把 Windows 原生路径当 Linux cwd。

## Windows 侧只需准备一次

1. 安装支持 RTX 4090 的 NVIDIA Windows 驱动；不要在 WSL 内安装 Linux GPU 驱动。
2. PowerShell 执行 `wsl --update` 和 `wsl --list --verbose`，确认 Ubuntu 使用版本 2。
   已有 Ubuntu 24.04 就复用；没有则执行 `wsl --install -d Ubuntu-24.04`，完成用户名设置。
3. 安装并启动 Docker Desktop，启用 WSL2 backend，并在 Settings → Resources → WSL Integration
   中启用该 Ubuntu。脚本复用它，不另装 Docker daemon。
4. Ubuntu 内执行 `nvidia-smi`、`docker info`、`docker compose version`。都成功后继续。
   若找不到前一命令，可用 `/usr/lib/wsl/lib/nvidia-smi`。

依据：[Microsoft 文件系统建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)、
[NVIDIA WSL 指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)、
[Docker WSL2 integration](https://docs.docker.com/desktop/features/wsl/)。

## 获取代码并一键配置

在旧电脑将需要迁移的代码和本次安装文件提交并推送到你自己的仓库；只存在于旧电脑工作区的改动不会出现在 clone 中。
本任务不自动推送仓库。

在 Ubuntu 终端执行（替换仓库地址和包含安装提交的部署分支；本次工作所在分支为 `cqy`）：

```bash
sudo apt-get update
sudo apt-get install -y git
mkdir -p ~/projects
cd ~/projects
git clone --branch 你的部署分支 你的仓库地址 assistant_agent
cd assistant_agent
bash scripts/setup_wsl.sh
```

安装会使用 sudo 安装系统依赖，可能要求输入 Ubuntu 密码；下载依赖和模型需要联网。
预留磁盘空间供 Python、CUDA 库、PyTorch、浏览器、模型下载缓存及 Docker 镜像使用。

脚本会复用 `hello_agent` conda 环境及其原路径；没有 conda 时安装固定版本、SHA256 校验的 Miniforge。
安装 Python 3.12、Node 22、ffmpeg 7，以及 `config/constraints-wsl.txt` 中固定的关键 Python 包。
约束文件固定关键版本，并非所有传递依赖或 apt/conda 包的完整锁文件。
现有环境也会安装这些版本；建议新电脑使用专用 `hello_agent`，不要放入其他项目依赖。

随后自动安装固定 Playwright MCP 和匹配 Chromium，复制配置模板，下载指定不可变 revision 的 SigLIP2，
导出图文联合 FP16 ONNX，执行两路 CUDA 实际推理；最后做 mock 图装配检查，并拉取/构建 Mem0 与 Qdrant 镜像。
构建镜像不启动 Mem0，不调用云端模型。

**默认不安装 BGE/FastEmbed，`visual_memory_search` 暂不可用。** SigLIP2 仍用于选帧和图文提醒；
Mem0 使用自身配置的云端 embedding，不依赖本地 BGE。

重复运行不会覆盖 `.env`、`.local/mcp_servers.json` 或已存在模型目录；已有模型必须通过校验，
不合格则报错，不删除重下。`--skip-model` 可跳过模型下载和 GPU 验收，用于先配置文本环境；
这不表示视觉已经就绪，后续重新执行不带该参数的安装即可。

## 填写配置与启动

`.env.example` 和 `config/mcp_servers.example.json` **随 Git 提供，不需要另行下载**。
安装只在目标不存在时创建 `.env` 和 `.local/mcp_servers.json`，文件权限为 600。

先保持默认 mock：

```bash
bash scripts/start.sh check
bash scripts/start.sh server
```

服务固定监听 `127.0.0.1:8089`，默认关闭热重载，Ctrl+C 停止。另一个 Ubuntu 终端进入仓库：

```bash
bash scripts/start.sh client --check
bash scripts/start.sh client
```

客户端工作目录默认 `~/assistant_cqy/studio`，浏览器使用 headless Chromium。
要换客户端 cwd，传 `--cwd`，并先用 `assistant-agent --install-browser --cwd 新目录` 安装对应浏览器。
Shell 默认关闭，需要时启动客户端加 `--allow-shell`，仍经过逐次工具审批。

要启用真实模型，在本机编辑 `.env`，至少填写：

```dotenv
MULTIMODAL_AGENT_PROVIDER_MODE=real
MULTIMODAL_AGENT_CHAT_PROVIDER=qwen
QWEN_API_KEY=你自己的密钥
QWEN_CHAT_MODEL=qwen-plus
```

视觉理解还要设置 `MULTIMODAL_AGENT_VISION_PROVIDER=qwen`、对应 `QWEN_VISION_API_KEY` 和账号可用的
`QWEN_VISION_MODEL`；其他业务 Tool 同样按 `.env.example` 显式配置。
本机 MCP 默认关闭，启用模板中的浏览器时设置 `MULTIMODAL_AGENT_MCP_ENABLED=1`。
`config/mcp_services.example.json` 另提供已有的百炼搜索、高德和 Gmail 模板，也随 Git 下载；
按需把其中的 server 项加入私有配置，填写 `.env` 中的 `QWEN_API_KEY`、`AMAP_MAPS_API_KEY`、
Google OAuth 字段并完成授权。只加入已经配置的服务，避免一个缺少凭据的 MCP 阻塞启动。
默认模板仅装配浏览器；安装不会把旧电脑第三方帐号和私人 MCP 地址自动迁移。

需要 Mem0 时，在填写密钥后设置 `MEMORY_BACKEND=mem0`，运行：

```bash
bash scripts/start.sh memory
bash scripts/start.sh server
```

`memory` 启动现有 Docker Compose 的 Mem0/Qdrant 并等待健康检查；使用期间会按照配置调用云端模型。
Qdrant 6333、Mem0 8890 均只绑定回环地址。当前认证只适合受信本机，不能改为公网暴露。

## 本地目录和 Git

| 内容 | 位置 | Git |
| --- | --- | --- |
| 源码、脚本、关键版本约束、无密钥模板 | `src/`、`scripts/`、`config/`、`.env.example` | 跟踪 |
| 密钥及真实服务设置 | `.env` | 不跟踪，需本机填写或私下安全复制 |
| 私有 MCP、Python/工作目录位置 | `.local/` | 不跟踪，安装自动创建 |
| SigLIP2、下载缓存、运行数据和主动投递数据库 | `.data/` | 不跟踪 |
| 官方 Agent Server checkpoint/Store | `.langgraph_api/` | 不跟踪，保留官方位置 |
| 浏览器、客户端会话和用户输出 | 用户 cwd 的 `.agent/`、`outputs/` | 不属于本仓库 |
| Mem0 记忆和 Qdrant 数据 | Docker `mem0_history`、`qdrant_data` volumes | 不属于 Git，单独备份 |
| 线程媒体临时文件 | `~/assistant_agent/threads/` | 不属于本仓库，按 TTL 回收 |

`.data` 内含数据库，**不是整个目录都可随意删除的缓存**。模型可重新准备，历史记录不能靠下载恢复。
安装默认新数据，不自动搬运旧会话、记忆或用户文件。模型禁止放入 Git；需要离线迁移时单独复制模型目录并执行 GPU 校验。

旧安装若还有 `.local/agent_server`、`.local/models`、`.local/generated`：停止服务并备份后，使用现有 Python：

```bash
python scripts/setup_local.py migrate-data
```

只在目标目录不存在时将完整目录移到 `.data`，遇到冲突立即报错；保留 SQLite 的 WAL 文件。
脚本使用同一 Server 锁，拒绝与 launcher 并发。直接手动启动的其他程序须由你先停止。
迁移不改写 `.env`：其中若显式引用旧目录，按提示改到 `.data` 后再启动，避免重建空数据库。

## 验收与边界

```bash
bash scripts/start.sh check                 # 无密钥、mock 三张原生图装配
bash scripts/start.sh gpu                   # 仅本地模型：checksum + CUDA image/text
bash scripts/start.sh client --check        # 检查现有 8089 服务和本机设备连接
```

GPU 检查使用合成图片和固定文本，不上传数据、不调用真实 Provider。mock 检查不读取 `.env`，隔离
用户 Home 和运行数据，不启动第二个 Server。图装配通过不等于实际对话质量或远端服务验证通过。
若已有 FastEmbed 却未下载 BGE，装配可能记录模型不可用；这是当前主动省略的视觉历史检索能力。

依赖下载中断：重新运行安装。模型目录存在但损坏：先人工备份/移走该模型目录，再运行安装。
`nvidia-smi` 成功但 GPU 推理失败：检查 Windows 驱动和 CUDA 12/cuDNN 9；安装使用
`onnxruntime-gpu[cuda,cudnn]` 提供用户态库，无需安装 Linux GPU 驱动。
[ONNX Runtime 版本兼容说明](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)。

新电脑的 WSL、4090、sudo 和 Docker Desktop 需要在目标机运行上述验收；旧电脑上的离线检查不能替代它们。

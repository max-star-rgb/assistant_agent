# Windows / WSL2 安装与迁移

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 新电脑安装、配置文件和本地数据迁移操作入口 |
| Owns | 依赖安装、手动配置迁移、模型准备、目录归属和安装验收 |
| Does not own | Graph/Tool/Memory 业务语义、视觉算法或公网生产部署 |
| 源码与 schema 入口 | `requirements.txt`、`scripts/setup_local.py`、`config/` |
| 验证入口 | `python scripts/setup_local.py check` |
| 相邻 authority | `agent-server-architecture.md`、`visual-perception-architecture.md`、`tool-calling-architecture.md` |

在新电脑的 **WSL2 Ubuntu** 中部署，项目放在 Linux 家目录。流程就是：
**Git 拉取 → 虚拟环境安装 requirements → 下载 BGE 和 SigLIP2 → 手动复制旧 .env → 启动。**

以下命令使用 Ubuntu 24.04、Python 3.12。Windows 安装 NVIDIA 驱动，WSL 内先确认
`nvidia-smi` 能识别 4090；不要在 WSL 内安装 Linux GPU 驱动。
参考 [NVIDIA WSL 指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)。

## 1. Git 拉取

先在旧电脑把需要迁移的提交推送到仓库；未推送的代码不会出现在新电脑上。本任务不会自动 push。

```bash
mkdir -p ~/projects
cd ~/projects
git clone --branch cqy git@github.com:max-star-rgb/assistant_agent.git
cd assistant_agent
```

这里使用包含本次改动的 `cqy` 分支；SSH 克隆需要新电脑已配置 GitHub SSH 访问。

## 2. 创建虚拟环境，安装依赖

```bash
# 仅缺少系统包时安装
sudo apt-get update
sudo apt-get install -y python3.12-venv ffmpeg

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

已有专用 conda 环境也可直接激活后执行最后两条命令，不需要再创建 venv。
requirements 包含服务、GPU 推理、BGE 和 SigLIP2 导出依赖，并复用项目关键版本约束。
BGE 使用 `fastembed-gpu`；不要另装 CPU 版 `fastembed` / `onnxruntime` 到同一环境。

## 3. 下载 BGE、准备 SigLIP2

在仓库根目录、已激活虚拟环境的终端执行：

```bash
python -c 'from fastembed import TextEmbedding; TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", cache_dir=".data/models/fastembed", local_files_only=False)'

export HF_HOME="$PWD/.data/models/huggingface"
python scripts/export_siglip2_embedding_onnx.py \
  --revision 75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2 \
  --output-dir .data/models/siglip2-base-patch16-224
```

BGE 用于视觉历史的文本向量检索（独立 Qdrant 索引）；SigLIP2 用于视觉 embedding。
SigLIP2 需要下载后导出图文联合 ONNX，现有导出脚本会使用 CUDA。目标目录已存在时拒绝覆盖；
已有完整导出结果时跳过导出，直接执行 GPU 验收。模型不通过 Git 传输。

## 4. 手动复制现有 .env

把旧电脑项目根目录的 **真实 `.env`** 私下复制到新电脑项目根目录。
保留现有密钥和业务配置，不需要重新填写模板。复制后核对模型路径：

```dotenv
VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR=.data/models/fastembed
SIGLIP2_MODEL_DIR=.data/models/siglip2-base-patch16-224
```

模型在其他目录时改为实际位置。其他旧电脑绝对路径、localhost 服务地址也按新电脑调整。
启用了 MCP 时，还需复制 `.env` 中 `MCP_CONFIG_PATH` 指向的私有配置
（通常为 `.local/mcp_servers.json`），检查其中的路径。
`.env.example`、`config/*.example.json` 是随 Git 提供的参考模板，无需额外下载。

## 5. 验收与启动

```bash
python -m pip check
python scripts/setup_local.py check
python scripts/setup_local.py gpu --model-dir .data/models/siglip2-base-patch16-224
python scripts/run_server.py --no-reload
```

mock 检查隔离真实配置，只装配三张原生图；GPU 检查使用合成输入进行本地 CUDA 图文推理。
启动服务会读取复制来的 `.env`，按其中配置运行。服务地址为 `http://127.0.0.1:8089`。
另一个 Ubuntu 终端进入仓库、激活同一个环境后运行：

```bash
source .venv/bin/activate
assistant-agent --check
assistant-agent --browser-headless
```

## 按现有配置补齐外部服务

Python requirements 不安装系统程序或启动数据库；仅在配置启用这些能力时处理：

- 浏览器 MCP：准备 Node.js 20+，执行 `assistant-agent --install-browser --cwd ~/assistant_cqy/studio`。
  使用仓库浏览器 MCP 模板时，在启动 Server 的终端设置
  `export PLAYWRIGHT_BROWSERS_PATH="$HOME/assistant_cqy/studio/.agent/browser-runtime/browsers"`；
  修改客户端 cwd 时也要同步浏览器安装位置。
- 本机 Mem0 / Qdrant：准备可在 WSL 使用的 Docker Compose，再执行
  `docker compose --env-file .env -f docker/mem0/compose.yaml --profile mem0 up -d --wait`。
  如果 `.env` 已指向可用的远程服务，直接复用。BGE 下载本身不会启动 Qdrant。
- 其他 MCP（例如 Google OAuth）：复制对应私有配置，并在新电脑完成所需授权。

## 哪些文件通过 Git 迁移

| 内容 | Git | 迁移方式 |
| --- | --- | --- |
| 源码、requirements、脚本、无密钥模板 | 跟踪 | Git 拉取 |
| `.env`、`.local/` 私有配置 | 不跟踪 | 手动复制 |
| `.data/models/` 模型 | 不跟踪 | 新电脑下载 |
| `.data/` 中的数据库、`.langgraph_api/`、Docker 数据卷 | 不跟踪 | 需要保留历史时停服务后单独备份迁移 |
| `.venv/` | 不跟踪 | 新电脑重建 |

`.local` 放私有配置，`.data` 放模型和运行数据，职责不同；`.data` 不是整个目录都可删除的缓存。
旧安装若有 `.local/agent_server`、`.local/models`、`.local/generated`，停服务并备份后可执行
`python scripts/setup_local.py migrate-data`，仅在目标不存在时迁移，并手动修改 `.env` 的旧路径。

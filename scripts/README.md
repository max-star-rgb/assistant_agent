# 脚本入口

新电脑准备：Git 拉取、虚拟环境安装
`requirements.txt`、下载 BGE 和导出 SigLIP2、手动复制现有 `.env`，再启动：

```bash
python scripts/setup_local.py check
python scripts/run_server.py --no-reload
# 另一个终端，激活同一个虚拟环境
assistant-agent --check
assistant-agent --browser-headless
```

## 服务与客户端

### 本机 V1 一键启动

在 PyCharm 中单独打开 `assistant_agent_cqy`，选择唯一的 Agent Server 启动配置 **Agent Server (Real)** 运行。
该配置使用 `hello_agent`，按顺序读取相邻 `assistant_agent/.env` 与本目录
`local/deployment.env`；后者覆盖 V1 的端口和上传暂存路径。两个文件均为本机私有配置，不能提交。
V1 启用远端视觉记忆时，私有覆盖文件需设置 `MEMORY_BACKEND=mem0`，不能沿用 V2 的 `disabled`。
启动脚本不再重复读取缺失的本目录 `.env`，默认关闭自动重载。
V1 的 Studio/SDK 服务地址为 `http://127.0.0.1:8089`，选择 `assistant-native-v4` 并新建会话；
媒体入口为 `ws://127.0.0.1:8089/agent-service/v1`。它不启动 V2 的 Gateway。
可与 V2 的 8088 后台并行；checkpoint 与运行锁各自保存在对应源码目录。
模型缓存及部分外部服务沿用已有配置，不代表所有业务数据完全隔离。
无需重新启动已在运行的 Mem0/Qdrant；Agent Server 启动前自动释放目标端口（默认 8089）。
依赖本机 `lsof`，先正常终止监听进程，5 秒未释放则强制结束；该端口上的其他服务也会被结束。
仓库锁被旧实例持有时，先等待 5 秒，再通过 `lslocks` 和 `/proc` 核实同仓库启动器并发送 SIGINT，
等待其清理子进程后接管锁。无法确认身份时不终止持锁进程。取得锁后才释放端口，避免未监听的旧实例遗漏清理。
本机 PyCharm 的 `Mem0` 配置使用本仓库 `scripts/run_mem0.py` 和本机未跟踪 `.env`，
管理现有共享 Mem0 容器与数据卷。使用本仓库旧式 `/memories` sidecar，不再挂载相邻仓库的快照版本实现。
启动时等待 Mem0 完成初始化和健康检查，最长 600 秒；期间暂时 `unhealthy` 不提前退出，
容器退出或等待超时仍明确失败。Docker 健康检查同样保留 600 秒冷启动宽限期。
看到 `Mem0 ready` 后再启动依赖记忆的对话；仅容器 Running 或端口监听不代表服务就绪。


- `run_server.py`：官方 `langgraph dev` 薄入口，负责启动前释放目标端口、单实例锁、端口检查、配置来源和仓库外日志。
  PyCharm 可继续使用 `hello_agent` 执行；默认重载，`--no-reload` 关闭。`--no-env-file` 忽略真实 `.env`。
  状态由官方 `.langgraph_api/` 保存，不启动自定义 Runtime 或 PostgreSQL。
- `agent_cli.py`：原生 SDK 对话客户端，支持 history/replay/rollback；状态变更遵守入口自身确认规则。
- `media_simulator.py`：连接既有 `/agent-service/v1` 的媒体协议模拟客户端；不会另启 Server。
- `setup_local.py migrate-data`：停止服务后把 `local` 的模型、generated 和 agent_server 目录移到 `data`；
  不合并目标、不删除历史数据、不改写 `.env`，迁移后按提示更新显式路径。
- `setup_local.py check`：隔离用户数据的 mock 三图装配与模板检查；不读密钥，不调用云端模型。
- `setup_local.py gpu`：SigLIP2 manifest/checksum 与实际 CUDA 图文推理；使用合成输入、不上传数据。

发行命令 `assistant-agent` 与服务在同一 Ubuntu 内使用，默认逐次工具审批；`--allow-shell` 显式允许当前
OS 用户权限的 Shell。客户端 `--install-browser --cwd 路径` 安装独立 Chromium，需自行准备 Node.js 20+。
默认工作目录 `~/assistant_cqy/studio`；改 cwd 时先为那个目录安装浏览器。

## Memory 与模型准备

- `run_mem0.py`：现有 Mem0/Qdrant operator 控制台；包含真实记忆读写和删除操作，按其自身确认规则使用。
- `run_qdrant.py`：现有 Qdrant supervisor；视觉文本检索需要 BGE 和 Qdrant。
- `migrate_mem0_memories_to_chinese.py`：显式真实 Provider 的记忆翻译迁移；不是新电脑安装步骤，不自动执行。
- `export_siglip2_embedding_onnx.py`：下载固定 revision 后导出图文联合 ONNX，已存在目标拒绝覆盖。
  按安装说明手动执行，固定 revision 为 `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`，
  目标 `data/models/siglip2-base-patch16-224`。
  `torch`、`transformers`、`onnxscript` 是导出依赖；运行只使用 ONNX Runtime GPU。
  `export_siglip2_vision_onnx.py` 为既有 deprecated 兼容入口，不用于新安装。

## 文档校验与已删除入口

`python scripts/check_documentation_authority.py --repo-root .` 检查 authority 路由。
原 `evals/` 已清理，依赖它的四个 `run_system_*` 包装脚本同时删除；不恢复旧 runner 或测试目录。
开发验证按 [tests/README.md](../tests/README.md) 在临时 feature 目录显式运行。

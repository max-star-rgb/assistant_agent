# 脚本入口

新电脑准备：Git 拉取、虚拟环境安装
`requirements.txt`、下载 BGE 和导出 SigLIP2、手动复制现有 `.env`，再启动：

```bash
python scripts/setup_local.py check
python scripts/run_server.py --port 8088 --no-reload
# 另一个终端，激活同一个虚拟环境
assistant-agent --check
assistant-agent --browser-headless
```

## 服务与客户端

Studio/SDK 的对话图为 `assistant-business-v2`。Studio 操作：连接 V2 的 `http://127.0.0.1:8088`，
选择该 graph 或基于它创建 Assistant，**新建 thread**，输入 `{"messages":[{"role":"user","content":"你好"}]}`。
Graph/Chat 可查看业务 Agent 的回答与工具执行；持续语音使用 `/media-lab` 原生 Realtime 会话。
需验证审批时把 context 的 `require_tool_approval` 设置为 true，用原生 interrupt/resume；取消使用原生 run cancel。
旧 native/Voice thread 不迁移，必须新建业务 thread。旧媒体 `chat` 仍明确拒绝。

本阶段默认 mock：业务图使用离线模型，原生 Realtime 连接不启用，不能据此评估真实语音质量。
真实评审另行授权后，显式配置 real 模式及独立 `QWEN_REALTIME_MODEL=qwen3.5-omni-flash-realtime`、
`QWEN_API_KEY`、`QWEN_REALTIME_BASE_URL`（无 query 的 wss 地址，本机使用 `wss://maas.qianwenaiapi.com/api-ws/v1/realtime`）、`VOICE_TIMEOUT_SECONDS=60`；业务模型仍使用原配置。
real 缺配置启动失败，不自动借用视觉 key，不回退 mock。模板见 `.env.example`，不要提交真实值。
移除 Voice 图后必须重启 `scripts/run_server.py`，仅源码重载不会刷新启动时复制的 Graph 注册配置。
同一工作目录只运行一个服务；本机 `cqy:8089` 与 `V2:8088` 使用独立 worktree 并行运行。现有实例为 real 或不可用时，不用 mock 测试结果宣称真实能力已验收。

### 本机多分支并行部署

当前本机映射：`/home/lenovo1/pycharm_project/assistant_agent` 是 `V2:8088`；
同级 `/home/lenovo1/pycharm_project/assistant_agent_cqy` 是 `cqy:8089`。
两者共用 `hello_agent` Python 环境，但分别加载各自 `src`；依赖版本发生冲突时需另行隔离环境。
`cqy` 使用 `assistant-native-v4`，V2 使用 `assistant-business-v2`，客户端须选择对应图并新建 thread。

本机用户级 systemd unit 位于 `~/.config/systemd/user/assistant-agent-{v2,cqy}.service`，
已按用户授权启用 real、自动重载、故障重启及用户登录后自启。systemd 按顺序读取原 V2
目录的 `.env` 和各 worktree 的 `local/deployment.env`；后者只保存分支端口、模型与数据路径等
非密钥覆盖项。`--no-env-file` 避免 CLI 再次加载 `.env` 覆盖这些分支设置，并非禁用真实配置。
旧的 `assistant-agent-server.service` 已停止并禁用自启，配置文件保留。
真实 `.env` 共享读取、不复制密钥。运行状态分别保存在各目录的 `.langgraph_api/`、`data/`，
日志为 `/tmp/assistant_agent/logs/agent_server-8088.log` 和 `agent_server-8089.log`。
这是进程与工作目录隔离，非 OS 沙箱：两者仍共享宿主用户、默认用户工作目录和
`~/assistant_agent/threads` 媒体暂存目录。为保留既有个人能力，Mem0 个人记忆、MCP 配置、
Provider 凭据和模型文件继续共享；Git worktree 不构成安全权限边界。
语音 Gateway 的 systemd 主 unit 仅依赖 `assistant-agent-v2`，drop-in 将连接地址指定为 `8088`，页面为
`http://127.0.0.1:3101/`；V2 自带语音页面为 `http://127.0.0.1:8088/media-lab`。
迁移 unit 依赖时必须修改主 unit，并通过 `systemctl --user show assistant-qwen-gateway -p Requires -p After`
核对实际依赖。drop-in 中空的 `Requires=` / `After=` 不能清除旧依赖；残留旧服务会因目录锁冲突
循环重启并连带中断 Gateway。旧服务应保持 inactive，而非只取消自启。

```bash
systemctl --user status assistant-agent-v2 assistant-agent-cqy
systemctl --user restart assistant-agent-v2
systemctl --user restart assistant-agent-cqy
systemctl --user stop assistant-agent-v2 assistant-agent-cqy
journalctl --user -u assistant-agent-v2 -n 50
```

日常直接访问现有服务，维护使用上面的 systemd 命令。PyCharm 的 **Agent Server (Real)**
仅改了 V2 端口，尚不包含 systemd 的分支环境覆盖；不要用它替代本部署，也不要同时启动造成目录锁冲突。
本次已验证两个分支的真实业务 Graph 回复；不把该连通性检查视为全部媒体与外部工具的能力验收。

- `run_server.py`：官方 `langgraph dev` 薄入口，保留单实例锁、端口检查、配置来源和仓库外日志。
  PyCharm 可继续使用 `hello_agent` 执行；默认重载，`--no-reload` 关闭。`--no-env-file` 忽略真实 `.env`。
  状态由官方 `.langgraph_api/` 保存，不启动自定义 Runtime 或 PostgreSQL。
- `agent_cli.py`：原生 SDK 对话客户端，支持 history/replay/rollback；状态变更遵守入口自身确认规则。
- `media_simulator.py`：连接既有 `/agent-service/v1` 的媒体协议模拟客户端；不会另启 Server。
- `setup_local.py migrate-data`：停止服务后把 `local` 的模型、generated 和 agent_server 目录移到 `data`；
  不合并目标、不删除历史数据、不改写 `.env`，迁移后按提示更新显式路径。
- `setup_local.py check`：隔离用户数据的 mock 三张注册图及内部业务子图装配与模板检查；不读密钥，不调用云端模型。
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

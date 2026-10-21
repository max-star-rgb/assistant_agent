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

外部媒体连接 **`ws://192.168.0.165:3101/agent-service/v1`**（其他机器使用实际宿主 IP）；8081 同路径也保留。
前台 Gateway 的同一个 Node 进程同时监听 `0.0.0.0:3101` 和 `0.0.0.0:8081`，
音频直接进入前台原生实时会话，VIDEO 复用后台内部视觉通道。Studio/SDK 仍用 `8088`。
`audioStreamV1` 必须显式协商。旧 `run_media.py` 和 `assistant-media-entry.service` 已退休。
只需启动 PyCharm **Realtime Server**（或 `python scripts/run_pycharm.py frontend`），即同时启动 3101/8081；
停止前台时两个监听与它们的连接一起关闭。后台 8088 须先运行，或使用 **Assistant (All)**。

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
同一工作目录只运行一个服务；本机只运行当前目录的 8088 后台。现有实例为 real 或不可用时，不用 mock 测试结果宣称真实能力已验收。

### 本机单实例部署

唯一后台源码为 `/home/lenovo1/pycharm_project/assistant_agent`，端口 8088。
网页和媒体 Gateway 共用它；默认用户工作目录为 `/home/lenovo1/assistant_cqy/`，不按入口分区。
`outputs/` 保存唯一正式产物，原生 Store 保存引用；`.agent/` 能力目录按需创建。
`assistant-agent-cqy.service`（8089）已停止并禁用自启，旧源码 worktree 保留，不参与运行。
旧 `assistant-agent-server.service` 同样保持停用，不删除历史配置或数据。

当前服务使用 `assistant-agent-v2.service` 与 `assistant-qwen-gateway.service`；Gateway 仅依赖前者。
系统服务与 PyCharm 启动方式二选一，不同时启动同一工作目录。原生状态位于 `.langgraph_api/`，
专项数据位于 `data/`，后台日志为 `/tmp/assistant_agent/logs/agent_server-8088.log`。
真实 `.env` 与 `local/deployment.env` 保持私有；CLI 的 `--no-env-file` 仅表示不重复加载 env。
网页地址为 `http://127.0.0.1:3101/`，媒体入口同时监听 3101/8081。

```bash
systemctl --user status assistant-agent-v2 assistant-qwen-gateway
systemctl --user restart assistant-agent-v2
systemctl --user restart assistant-qwen-gateway
systemctl --user stop assistant-qwen-gateway assistant-agent-v2
journalctl --user -u assistant-agent-v2 -n 50
```

日常直接访问现有服务，维护使用上面的 systemd 命令。需要由 PyCharm 管理进程时，直接选择 `.run` 中的配置：

- **Agent Server (Real)**：后台 Agent Server，端口 `8088`，读取根 `.env` 后叠加 `local/deployment.env`。
- **Realtime Server**：前台 Realtime Gateway，同一进程监听 `3101` 和 `8081`，读取 `integrations/qwen_audio_agent/.env`，固定连接后台 `8088`；启动前等待后台就绪。
- **Assistant (All)**：一键启动后台与前台（含媒体 8081）；先等待 `/qwen-audio/capabilities` 就绪再启动前台。`/ok` 只表示 HTTP 可用，不能代表 Graph 已就绪。点击停止关闭所有子进程，任一进程退出时也清理其余进程。

这三个配置共用 `scripts/run_pycharm.py` 和 `hello_agent` Python 环境；前台自动选择本机已安装且版本兼容的 Node，无需 PyCharm Node.js 插件。
启动器先检查本地配置文件，再停止与当前工作目录匹配的 systemd 实例释放端口，随后由 PyCharm 管理进程及其子进程。
启动器默认自动清理，不带 `--clear-ports` 的旧 IDE 配置或命令也会执行；该参数保留兼容：后台清理 8088，前台清理 3101/8081，All 清理三者。
先停止同项目对应角色的旧启动器（含 `all`，因此也会停止其另一角色），再向剩余 TCP 监听进程发送 SIGTERM；
等待 5 秒仍占用时发送 SIGKILL，再等待 5 秒，失败则报错。清理会结束目标端口上的其他程序，
当前连接也会断开。需要本机安装 `lsof`，权限不足时明确失败。
后台 systemd 被停止时，其依赖的 Gateway 也会停止；单独启动后台后如需语音，运行 **Realtime Server**，或直接使用 **Assistant (All)**。
停止 PyCharm 运行后不会自动恢复 systemd；需要恢复常驻运行时执行 `systemctl --user start assistant-agent-v2 assistant-qwen-gateway`。
不修改 systemd 自启设置。不要同时运行 **Assistant (All)** 和两个独立配置。
本次已验证两个分支的真实业务 Graph 回复；不把该连通性检查视为全部媒体与外部工具的能力验收。

- `run_server.py`：官方 `langgraph dev` 薄入口，保留单实例锁、端口检查、配置来源和仓库外日志。
  PyCharm 可继续使用 `hello_agent` 执行；默认重载，`--no-reload` 关闭。`--no-env-file` 忽略真实 `.env`。
  状态由官方 `.langgraph_api/` 保存，不启动自定义 Runtime 或 PostgreSQL。
- `run_pycharm.py backend|frontend|all`：IDE 进程启动与停止入口，复用现有后台启动脚本和前台 Gateway，不创建新 Runtime。
- `agent_cli.py`：原生 SDK 对话客户端，支持 history/replay/rollback；状态变更遵守入口自身确认规则。
- `media_simulator.py`：连接既有 `/agent-service/v1` 的媒体协议模拟客户端；不会另启 Server。
- `setup_local.py migrate-data`：停止服务后把 `local` 的模型、generated 和 agent_server 目录移到 `data`；
  不合并目标、不删除历史数据、不改写 `.env`，迁移后按提示更新显式路径。
- `setup_local.py check`：隔离用户数据的 mock 三张注册图及内部业务子图装配与模板检查；不读密钥，不调用云端模型。
- `setup_local.py gpu`：SigLIP2 manifest/checksum 与实际 CUDA 图文推理；使用合成输入、不上传数据。

发行命令 `assistant-agent` 与服务在同一 Ubuntu 内使用，默认逐次工具审批；`--allow-shell` 显式允许当前
OS 用户权限的 Shell。客户端 `--install-browser --cwd 路径` 安装独立 Chromium，需自行准备 Node.js 20+。
默认工作目录 `~/assistant_cqy/`；改 cwd 时先为那个目录安装浏览器。

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

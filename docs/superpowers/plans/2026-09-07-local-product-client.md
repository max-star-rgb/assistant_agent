# 本机安装客户端接入计划

目标：安装后的命令行客户端连接唯一 8089 Agent Server 的现有生产图，在用户 cwd 工作，支持原生审批与会话续接。

本次范围：同机体验，采用现有受信本机身份。客户端主动连接执行 WebSocket，生产默认 composition 按公开 device_id 路由；已绑定设备不可回退到服务端。本机 Git、浏览器、Skills、媒体继续使用相同实际 cwd。注册与连接只允许回环、无浏览器 Origin；跨电脑、正式帐号登录和桌面安装器不在本次开放范围。

无需重复下载源码；构建 wheel 后安装到 cwd 下独立体验环境，检验发行模块与启动入口。保持唯一 PyCharm 8089；不另起 Agent Server，不调用真实 Provider。

- [x] 在 tests/tdd/device-production 编写生产路由、cwd 绑定、断连及非法远端注册的 RED 实验。
- [x] 将 broker 挂到现有 FastAPI，新增公开 device_id、本机注册检查和默认 backend 分发；本机客户端使用真实路径语义。
- [x] 提供安装后 assistant-agent 客户端入口，自动连接设备、原生 SDK 对话/HITL、线程续接；使用原生取消。
- [x] 构建并安装 wheel 到用户 cwd 下的体验环境；检查发行导入路径及 8089 实际连接，用原生图实验写入/读回哨兵文件验证执行路径；8089 仅进行安装客户端连接与 schema 检查。
- [x] 完成离线测试、文档校验、服务热重载健康检查，记录启动命令和明确限制。

验证：实验套件、现有 gateway/runtime/context/tool 核心测试；文档 authority validator；CLI --help；发行安装与本机执行连接检查。真实模型对话质量不由 mock 验证替代。

## 验证结果

- Tests: 62 passed，15.01 秒；一项已知 fastembed GPU 不可用转 CPU warning。实际命令：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/device-execution tests/tdd/device-production tests/core/contract/test_gateway_contract.py tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/contract/test_tool_contract.py --tb=short
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/assistant_cqy/start-assistant.sh --check
git diff --check
```

- Core invariant: IDENT-001 增加公开 device_id；LOOP-001 / CTX-001 增加同机设备绑定与角色 Tool 前连接检查，更新既有字段/装配契约测试。临时实验仍在默认忽略的 tests/tdd 下。
- 文档 validator valid=true，已复核所有列出的 owner；媒体 wire、观测和原生 run 生命周期未改变。
- 现有 8089 热重载后，system Assistant UUID 的 schemas 包含 device_id，/ok 返回 ok=true。
- 已构建 wheel：`/home/lenovo1/assistant_cqy/.agent/install/assistant_agent-0.1.0-py3-none-any.whl`。
- 安装副本：`/home/lenovo1/assistant_cqy/.agent/client-venv/lib/python3.12/site-packages/assistant_agent/client.py`；确认不是源码 editable import。
- 该 venv 使用 --system-site-packages 复用 hello_agent 的现有依赖；此次未证明干净机器完整依赖安装。
- 安装脚本 --check 实际连接唯一 8089，身份 lenovo1、cwd `/home/lenovo1/assistant_cqy`。未调用真实模型/Provider，未验证真实模型任务质量。
- 原生图离线实验验证生产 WorkingDirectoryBackend 经真实 socket 写入两个独立客户端目录、HITL 前无写入、客户端审批 payload 恢复原生图并读回；不是第二个 Agent Server。
- 审查修复了 PATH 丢失导致客户端 Python/CLI 环境错误的问题，并完成 RED/GREEN 回归。

## 现在如何体验

运行 `/home/lenovo1/assistant_cqy/start-assistant.sh`，进入命令行对话。启动器显式开启 Shell 能力，但每次受治理工具调用仍需要 y 审批。
服务端必须保持 8089 在线；退出客户端后本地执行连接关闭。会话 ID 保存在 cwd/.agent，下次启动续接服务端同一会话。
无需为了本机体验先制作桌面安装器。当前已安装的 wheel 包含完整 Python 包，但客户端不启动模型或 Agent loop；尚未拆分成精简客户端发行包。

限制：只开放同机回环；身份仍是本机开发身份而非正式帐号登录。Git/浏览器/媒体/Skills/项目指令仍在服务端使用同一实际 cwd，设备 binary upload/download 未实现。
下一步才是跨机执行适配和正式帐号认证；不能通过解除回环检查冒充已经支持其他电脑安装。
改动保留工作区供复核，未提交、未 push。

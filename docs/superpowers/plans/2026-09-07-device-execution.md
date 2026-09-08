# 客户端执行 backend 实验计划

**目标：**验证统一原生 AssistantAgent 可以通过设备 backend 在不同客户端目录工作，保持原生 ToolNode、HITL 和 checkpoint。

**方案来源：**本任务已确认的“产品 UI + 本地执行组件；统一 Agent Server 管理推理和会话”。本文不重复设计审批。

**实验边界：**先交付可复用 backend、反向 WebSocket 连接、本地执行组件与可重复离线实验。生产默认 composition 不切换，不另起 `langgraph dev`，不新增 Graph identity。正式帐号、产品 UI/安装包、Git/浏览器/媒体远端化属于后续产品接线，不能声称本实验已经完成这些能力。

**原生复用：**`SandboxBackendProtocol`（接口不代表实现具备沙箱）、`LocalShellBackend` 文件操作、现有 `build_assistant_agent`、原生 `ToolNode`/HITL/checkpointer。连接层只拥有连接与 RPC，不拥有会话/run 状态机。

**安全约束：**静态可信装配的 `(owner, device, token)` 授权；token 不进入模型/state；客户端主动连接；不允许网络输入决定 cwd；文件工具虚拟根是客户端批准的目录。Shell 默认禁用，显式启用表示接受当前 OS 用户完整权限，不宣称目录沙箱。执行无自动重试；断连/超时结果可能不确定。实验仅 Linux/POSIX Shell 取消，其他系统失败关闭。默认 mock，不调用真实 Provider。

## 实施任务

- [x] 1. 在 `tests/tdd/device-execution/` 写行为实验并确认 RED：两客户端同名路径写入互不串目录、错误凭据拒绝、未连接不回退、越界文件拒绝、重复请求不重放、Shell 取消清理进程。
- [x] 2. 在 `runtime/device_backend.py` 实现窄协议与反向连接 broker，按原生 authenticated identity 选择设备；在 `runtime/device_client.py` 实现本地组件，复用官方文件 backend，给可取消 Shell 增加进程组生命周期。
- [x] 3. 用现有 `build_assistant_agent` 注入设备 backend，显式 scripted mock 模型驱动原生文件 Tool，验证原生 HITL 批准前无写入、批准后客户端落盘、同一图多身份隔离。测试不替代真实模型能力评测。
- [x] 4. 提供 `python -m assistant_agent.runtime.device_client --help` 入口及实验 README；同步 tool authority 和 manifest，说明未接入生产默认链路与后续迁移边界。
- [x] 5. 运行定向 TDD、相关现有核心契约、文档 validator、检查源码 reload 后的现有 8089 `/ok`；整理结果与限制。

## 验证命令

统一 Python：`/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/device-execution
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m assistant_agent.runtime.device_client --help
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

实验采用回环 WebSocket 和临时目录模拟两台机器，不证明真实跨电脑部署、网络安全审计、全平台支持或正式多租户上线。

## 实验结果（2026-09-07）

- 13 项客户端执行实验及 13 项既有核心测试全部通过（26 passed，12.58 秒）。全程 mock/offline，无真实 Provider 调用。
- 实际验证命令：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/device-execution tests/core/contract/test_tool_contract.py tests/core/integration/test_runtime_lifecycle.py
```

- Core invariant: 未修改核心不变量或永久测试；验证既有工具契约与运行生命周期。
- 测试有一项 fastembed CUDA provider 不可用、使用 CPU 的 warning，不影响通过。
- CLI `--help` 成功；文档 authority validator `valid: true`，已复核 documentation-index/tool-calling owner；`git diff --check` 通过。
- 现有 8089 日志在 08:19:12 UTC 记录 device_client.py 热重载；随后 `/ok` 返回 `{"ok":true}`。未启动第二个 Agent Server。
- 审查回归已覆盖启动前取消槽位泄漏、重复取消时文件写入串行性、FIFO 阻塞、原生目录删除和 edit 内容膨胀。
- 代码与计划保留工作区供复核，未提交；实验测试位于仓库默认忽略的 `tests/tdd/device-execution/`。

结论：原生 backend 接缝可支撑服务侧 Agent 调用客户端文件/Shell，且可保留原生审批与身份隔离。
这只验证回环网络和独立进程，不等于正式统一帐号、跨机持久化或完整安装产品已完成。
下一步统一执行目标与各工具的路径/产物归属，再接入生产 Agent Server；避免文件在客户端而 Git/媒体仍误在服务端执行。

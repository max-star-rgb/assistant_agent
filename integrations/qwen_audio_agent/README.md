# Qwen Audio Agent 实验接入

独立 Node Gateway + 既有 Agent Server 的薄适配。此入口尚未替换 `/media-lab`，真实语音、原生 Server 联调和 WebUI 逐次审批尚未验收；不能据离线通过宣称 M1/M2 完成。

## 已实现与边界

- 锁定 `qwen-audio-agent@2.0.1`，通过公开 `backend-adapter-sdk`、`gateway-application` 组合。启动时对已发布 WebUI 的视觉状态文案应用一处版本受限的兼容补丁：未提供画面不等于此前有过画面，不主动播报此状态。补丁可重复执行，重装依赖后自动应用；版本或目标变化时明确失败，升级时必须复核。其余依赖逻辑保持原样。
- 一个 Gateway task 对应一个 `assistant-business-v2` thread，不 rollback 其他任务；Gateway 自身仍控制排队与并发。
- 业务只接收规范化委派文字，固定关闭业务 Memory、开启原生 HITL。暂不接收附件，明确返回 unsupported，不静默丢弃。
- 查询、取消和原生输入恢复；失败、提交未知、观测超时分别报错，不重投不确定的副作用请求。
- Gateway 挂断/关闭只停止观测，原生业务继续。重启后可以用任务查询命令对账，不保证自动恢复旧语音上下文或自动播报。
- 语音 Memory 按需召回；完成用户 ASR 按 ID 去重，提交到既有 delayed Memory Graph。上游自身的 Memory、偏好学习、摘要、定时提醒、知识库、直接联网和 MCP/OpenAPI 默认不启用。
- 日志只输出事件名、任务标识、状态和耗时，不输出上游错误正文、完整任务或媒体；上游自身的任务/会话状态保存在仓库外数据目录，按其保留策略管理。

**审批限制：** 发布版 Gateway 会把“任务授权／会话授权”转换为 Adapter 的 `once` 并自动批准后续操作。为保持本项目逐次 HITL，Adapter 拒绝所有前台授权响应；请用同一身份在 Studio 审核原生 thread。前台会保持等待，Studio 恢复后自动跟踪新 run。WebUI 审批按钮出现失败是明确的兼容限制，不要反复点击或改为全权限。完整 WebUI 逐次审批是 M1 的剩余阻塞项。

原生补充输入只支持单个 interrupt 的值为 `{"type":"input","mode":"text","prompt":"..."}`；回应必须为非空 `text`。不把普通最终回答中的问句识别为 interrupt；未知 interrupt 留待 Studio 处理。本接入没有向业务图添加新的提问工具。

## 环境与离线检查

**取消限制：** 仅对仍在执行的 run 提交取消。已终结的 run 返回 `business_already_finished_query_Server_result`；存在 checkpoint interrupt 时返回 `interrupt_requires_Studio_resolution`，请在 Studio 处理。上游 TaskManager 会把取消拒绝显示为任务失败并停止前台观察，因此该显示不代表原生业务失败；请查询原生结果。此兼容问题尚未解决，不作为完整生命周期验收通过。

需要 Node `^22.22.2 || ^24.15.0 || >=26.0.0`、npm 10+。本机系统 Node 18/npm 9 不满足要求。可以使用本机已有 Node：

```bash
export PATH=/home/lenovo1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH
cd /home/lenovo1/pycharm_project/assistant_agent/integrations/qwen_audio_agent
npx --yes npm@10.9.4 ci --ignore-scripts --no-audit --no-fund
node --test test/*.test.mjs
```

`--ignore-scripts` 适用于本接入使用的云端 WebSocket/WebUI 能力；本接入不启用依赖中的本地语音或桌面原生模块。npm 下载与测试不调用模型。Node 测试使用临时本机 HTTP fixture，真实 Gateway 仅绑定临时端口，不连接现有 `8089`。

Python 桥接测试：

```bash
cd /home/lenovo1/pycharm_project/assistant_agent
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/qwen-audio-agent
```

该 pytest 目录是项目约定的可删除临时测试。Node 包内测试含官方 BackendPort 一致性套件、真实上游 TaskManager 到 HTTP fixture 的委派路径，以及真实 conversation record 回调到 Memory 桥的路径。HTTP fixture 验证适配协议，不代替真实 Agent Server/模型验收。

## 真实验收启动

2026-10-15 本机试用配置：已在用户授权下启用真实 Provider，完成官方 WebUI 文本对话和原生业务委派连通性检查，并在 `assistant-agent-runtime` 查询到成功的业务 trace。麦克风采集、真实打断与完整 HITL 尚未验收；语音前台仍未接入 LangSmith。

本机 `assistant-agent-v2.service`、`assistant-qwen-gateway.service` 已配置为 systemd 用户服务，登录后自动启动。Gateway 主 unit 仅依赖 `assistant-agent-v2.service`，部署 drop-in 将目标覆盖为 V2 的 `http://127.0.0.1:8088`，旧 `assistant-agent-server.service` 已停止；`8089` 属于独立 cqy 实例。不能用 drop-in 的空 `Requires=` 清除主 unit 的旧依赖，须核对 systemd 合并后的依赖，避免旧服务重启连带中断 Gateway。直接打开 `http://127.0.0.1:3101/`；首次语音使用需允许麦克风。维护时使用 `systemctl --user restart assistant-qwen-gateway`；停止整套试用使用 `systemctl --user stop assistant-qwen-gateway assistant-agent-v2`。不要同时从 PyCharm 启动同一目录的服务。

仅在明确授权真实测试后填写未跟踪 `.env`。先确认目标 V2 Agent Server 位于 `8088`，手动启动时显式设置 `ASSISTANT_AGENT_URL=http://127.0.0.1:8088`，并按项目规则配置其 Provider 模式；本包不会启动、重启或自动切换 Python Server。

```bash
cp .env.example .env
# 手动配置 key，并显式设置：
# MULTIMODAL_AGENT_PROVIDER_MODE=real
# QWEN_AUDIO_ENABLE_REAL=1
node --env-file=.env src/gateway.mjs
```

打开 `http://127.0.0.1:3101`，使用随发布包提供的官方 WebUI。默认 mock 会明确拒绝启动 live Gateway，key 已存在也不会自动启用。指定模型不可用时保留失败，不切换其他模型。

日常使用固定打开 `3101`，无需输入 V2 的服务端口。Gateway 的代码默认值、模板和本机配置均指向
`http://127.0.0.1:8088`；`8089` 属于 cqy。页面显示“待命”时可直接输入文字发送；语音点击一次
“开启麦克风”，首次使用需浏览器授权。页面加载不会自动开始采集麦克风。
`/media-lab` 暂留作诊断与回退；在 Gateway 的真实通话、打断、业务委派及审批边界完成验收前不删除。

Gateway 使用独立配置/数据目录，忽略继承的其他 Provider、代理与 LangSmith 配置，仅投影本包选定的配置。此启动器是独立进程入口，不应在同一 Node 进程中嵌入多个 Gateway。默认只监听 `127.0.0.1`，不开启公网、LAN、Tailscale、WebRTC 或多用户。

默认状态目录为 `~/.local/state/assistant_agent/qwen_audio`。其中 `integration-id` 隔离不同安装下的顺序 task 编号；请与整个状态目录一起备份，不单独删除或替换。Agent Server 的数据持久性仍受其当前 in-memory 部署约束。

停止使用 Ctrl+C；这不会撤销已完成副作用，也不会取消已受理业务。需要取消时使用任务取消操作。观测超过 1 小时结束本地等待，业务可能仍在运行，应查询后处理。

## 查询与 Studio 审批

使用相同 `.env` 和状态目录，根据 WebUI 中的任务编号查询：

```bash
node --env-file=.env src/task.mjs task_1
```

命令只读取原生 thread/run，输出 `thread_id`、`run_id` 和状态，不调用 Provider。用 `thread_id` 在已有 Studio 中打开对应业务 thread，以同一 `ASSISTANT_AGENT_USER` 身份处理原生 HITL。普通语音打断不等于取消业务。

原生正常图 interrupt 的 run 也可能为 `success`；`interrupted` 还可能表示已停止，必须结合 checkpoint 判断待审批或待补充状态。查询结果为 `submission_unknown` 或提交报错时，不能自动重复执行。相同 task ID 的不同指令会被拒绝，避免错误返回旧任务结果。

## Memory 桥

Python custom routes 位于 `/qwen-audio/capabilities`、`/qwen-audio/recall`、`/qwen-audio/transcripts`，只允许已认证的本机请求；请求正文不能指定用户身份。它们复用 `VoiceTasks` 的召回与 `submit_memory_extraction()`，不执行任意 Tool/Graph。

Node 仅从上游完成转写的 `voice-user` record 收集文本，不包含模型回复、任务结果或工具内容。每会话最多 128 项、64,000 字；超过预算明确报告 `memory_snapshot_limit`，不静默截断。网络失败通过 `memory.error` 日志和工具源 health 暴露，普通会话继续。Node 不额外重试已提交快照，Python 复用既有三次重试及 30 分钟 rollback 调度；不承诺 exactly-once 写入。

尚未接回原生 Voice 的直接百炼搜索，复杂搜索可委派业务图。实时摄像头、视觉提醒与历史找物属于计划后续 V1；本包不绕过或替换已有并行视觉流水线。

## 当前交付状态与回退

- 任务 0—3：版本锁定、Gateway 组合、Adapter 和主要生命周期已有离线证据；真实语音与原生 Server 集成尚待验收。
- 任务 4：补充输入与 Studio 恢复已离线验证；WebUI 逐次审批未完成。
- 任务 5：Memory 薄桥已实现并离线验证；真实后台记忆提取及直接搜索尚未验收/接入。
- 任务 6：配置、查询和回退说明已提供；尚未切换默认入口。
- 任务 7：持续视觉未实施。

回退时停止此 Gateway，继续使用 V2 的 `http://127.0.0.1:8088/media-lab`。不同时让两套入口持有同一个语音会话。当前实验接入不改变 Studio/SDK 的业务图和旧语音入口。

外部依据：[官方 Backend Adapter SDK](https://qwenaudio.github.io/qwen-audio-agent/reference/backend-adapter-sdk)、[发布仓库](https://github.com/QwenAudio/qwen-audio-agent)。实现以锁定发布包的实际接口为准。

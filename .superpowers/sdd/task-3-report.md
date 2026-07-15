# Task 3 报告：Gateway freshness barrier 与兼容性

## Status

完成。

## 实现

- 将 Agent-Service 当前镜头 freshness barrier 的默认总预算从 `4.0` 秒收敛为 `1.5` 秒。
- 保留既有单个 `asyncio.timeout(...)`：promotion 与 sequence wait 共享同一总预算，没有分别获得 1.5 秒。
- 扩展常见当前镜头指代：眼前、画面、摄像头、镜头、看到/看见什么、这/那个是什么、手里、桌上、旁边、前面、后面、左边、右边、当前场景。
- 空间方向词要求与视觉指令或询问短距离共现，避免“前面提到、后面讨论”等普通对话误触发 freshness barrier。
- 未改变 timeout 路径：仍只消费共享 observer snapshot；最多 promotion 一次；超时继续携带 target/snapshot sequence、gap、waited_ms、`satisfied=false`，不创建前台或第二个 Qwen 调用。
- 未改变 provider token stream、Media `message`/`body` 协议、前台工具目录或普通上传/API `video_understanding` 路径。

## TDD 记录

测试决策：EXTEND，扩展现有最窄权威测试 `tests/scopes/gateway/test_agent_service_websocket.py`。

### RED 1

- 新增 1.5 秒默认 budget 与常见视觉指代参数化测试。
- 结果：`9 failed, 8 passed`。
- 正确失败原因：常量仍为 `4.0`；手里、桌上、旁边、前面、后面、左边、右边、当前场景未命中。

### GREEN 1

- 最小修改常量与视觉 reference regex。
- 直接相关 freshness、promotion、sequence wait、timeout、greeting 测试：`23 passed`。

### RED/GREEN 2（自审）

- RED：新增普通叙事方向词不触发视觉 barrier 的回归测试，确认“前面提到的方案，后面继续讨论”被裸方向词误命中：`1 failed`。
- GREEN：收紧空间方向匹配上下文；视觉指代与问候/非视觉边界：`19 passed`。

## 验证摘要

- 聚焦回归：`279 passed in 16.01s`。
  - Agent-Service WebSocket
  - H.264 ingestion、realtime video memory/observer、video context
  - context renderer、foreground tool catalog
  - runtime/provider stream
  - native tool handoff
  - 普通 `video_understanding` tool 与 input builder
- Agent-Service WebSocket 最终态：`64 passed in 4.48s`。
- critical 最终态：`199 passed in 12.84s`。
- gateway scope 在首次最终验证中完整通过；后续最后一次组合重跑被外部中断，未作为新增通过证据。
- `git diff --check` 通过。
- 全程 mock/local/offline；未调用真实 Provider，未新增依赖，未修改 `.env`。

## Video ID wire 边界

Agent-Service wire body 没有 video ID switching 字段。`H264VideoIngestionService` 只接受连接绑定的 `session_id`，并通过 `video_id_for_session(session_id)` 生成稳定 opaque video ID；同一连接/session 的不同 `videoIndex` 只是 frame index，不会切换视频会话。因此本任务没有发明新的 switching 协议。现有入口仍保留对异常 ingestion identity mismatch 的防御性 observer rotation，但正常 wire contract 每连接只有一个活动 video ID。

## Concerns

- 视觉指代识别仍是入口层的窄正则 gate，不是通用语义分类器；本次只覆盖验收要求的常见表达并保护明显的叙事方向词误触发。
- freshness barrier 是协作式 deadline；已启动的共享 observer promotion 可在 caller deadline 后继续由后台 observer 持有，这正是避免取消/重复 Qwen 执行的既有设计。

## Review follow-up：位置词 false positive

- Review 发现位置词分支仍会把 `看看前面的设计方案`、`描述后面的实施步骤` 与 `左边的代码为什么报错` 当作当前镜头指代。
- RED：新增参数化测试同时断言 helper 返回 false，且完整 Agent-Service chat 路径不调用 observer `promote` / `wait_for_snapshot_sequence`；结果 `3 failed, 16 passed`，三个失败均为 regex 误命中。
- 修复：移除“视觉动词 + 任意 12 字符 + 位置词”分支；位置词现在必须直接进入 `有/是/放着/拿着/摆着/站着/出现` 等 live visual object/question 结构，不再通过任意字符跨到“为什么”中的“什么”。
- GREEN：既有直接 freshness/greeting/timeout 回归 `23 passed in 1.58s`；普通位置/方向文本负例 `4 passed in 1.36s`。
- 正例继续覆盖 `看看桌上有什么`、`手里有什么`、`右边是什么`、`描述当前场景` 等当前镜头问法。

# 个人实时通话助理 Tool Roadmap

本文记录第一阶段个人实时通话助理的任务样例、最小工具集和扩展边界。目标是先覆盖高频个人事务，而不是一次性做大而全工具库。

## 阶段目标

- 默认离线安全：本阶段默认只注册 mock/local adapter，不因环境里存在 key 自动启用真实 Provider。
- 工具保持薄适配：Pydantic input/output schema、adapter/service、`ToolResult` 和 prompt-safe observation。
- 副作用统一治理：所有工具仍必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 多步骤流程沉淀为 `workflow_skill_v1` manifest，不新增通用 `run_skill`，也不绕过工具治理。

## 代表任务

### 早晨/出门简报

用户话术：

- “早上好，帮我说下今天出门前要注意什么。”
- “我今天上午有什么安排？天气怎么样？”
- “出门前给我一个简短 briefing。”

期望工具序列：

1. `calendar_search` 查询当天日程。
2. `weather` 查询当前位置或用户指定地点天气。
3. `web_search` 可选查询用户明确要求的新闻或最新信息。

成功输出形态：按时间顺序简述日程、天气影响和需要提前准备的事项；引用 web 结果时保留来源 URL/日期。

### 会议安排

用户话术：

- “帮我约 Alex 明天上午产品同步。”
- “找个我和 Alex 都方便的时间开会。”
- “把周五下午的评审会加到日历里。”

期望工具序列：

1. `calendar_search` 查询已有日程或空闲上下文。
2. `contacts_search` 解析参会人候选。
3. `calendar_create` 在用户确认后创建日程。

成功输出形态：先说明候选时间和参会人解析结果；写入前返回待确认摘要；确认后返回日程 `output_ref`。

### 通话中记待办

用户话术：

- “提醒我下午给客户回电话。”
- “把刚才说的上线前检查记成待办。”
- “这件事明天上午十点提醒我。”

期望工具序列：

1. 模型从当前对话抽取标题、可选时间和备注。
2. `reminder_create` 在用户确认后创建提醒。

成功输出形态：确认前说明将创建的提醒；确认后返回 reminder id/reference，不声称已经发送短信或邮件。

### 出门前建议

用户话术：

- “我现在出门来得及吗？”
- “下班前提醒我今天还要去哪。”
- “出门要带伞吗？我下午有什么安排？”

期望工具序列：

1. `calendar_search` 查询接下来日程。
2. `weather` 查询目的地或当前位置天气。

成功输出形态：给出是否需要提前出发、是否带伞/外套、以及下一场日程的时间提醒。本阶段不做地图路线计算。

### 网页资料讲解

用户话术：

- “查一下这个网页，给我讲重点。”
- “搜一下最近关于这个产品的新闻。”
- “打开这个 URL，帮我总结并给出处。”

期望工具序列：

1. `web_search` 查找当前信息或用户未给 URL 时找候选页面。
2. `web_fetch` 抓取用户给定或搜索返回的具体网页。

成功输出形态：用简短段落解释重点，给出来源 URL；不复制长篇原文。

### 购物决策

用户话术：

- “帮我找一双通勤白鞋，性价比高一点。”
- “这个东西怎么买更划算？”
- “比较一下这些商品的价格和评价。”

期望工具序列：

1. `shopping_search` 查询商品候选并完成报价比较。
2. `web_fetch` 可选读取用户指定详情页或评价页。

成功输出形态：列出候选、价格、URL 状态和推荐理由；不下单、不支付。

## 第一批工具

- `weather`：外部只读，查询指定地点当前/短期天气；默认 mock adapter。
- `calendar_search`：外部只读，读取用户日程；trace/audit 默认脱敏。
- `contacts_search`：外部只读，读取联系人候选；trace/audit 默认脱敏。
- `calendar_create`：外部写，必须由 runtime confirmation 放行，并要求 idempotency key。
- `reminder_create`：外部写，必须由 runtime confirmation 放行，并要求 idempotency key。

## MCP-backed 真实使用路径

默认注册仍使用 mock/local adapter。真实个人数据源只能在 `provider_smoke` / `pilot` runtime profile 下显式启用：

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_PERSONAL_ASSISTANT_PROVIDER=mcp
export MULTIMODAL_AGENT_MCP_ENABLED=1
export MULTIMODAL_AGENT_MCP_CONFIG_PATH=.local/mcp_servers.json
```

`.local/mcp_servers.json` 保持未跟踪，示例形态：

```json
{
  "servers": [
    {
      "server_name": "google_workspace",
      "preset": "google_workspace",
      "transport": "stdio",
      "command": ["google-workspace-mcp"],
      "personal_assistant_tools": {
        "calendar_search": "search_events",
        "calendar_create": "create_event",
        "contacts_search": "search_contacts"
      }
    },
    {
      "server_name": "todoist",
      "preset": "todoist",
      "transport": "stdio",
      "command": ["todoist-mcp"],
      "personal_assistant_tools": {
        "reminder_create": "create_task"
      }
    }
  ]
}
```

内置 MCP preset 只提供常见 allowlist/read-only/default visibility 和 personal tool 映射默认值；实际外部 server 的命令、参数、凭据和工具名差异仍以本地配置覆盖。Notion 和 Slack 当前作为直接外部 MCP 工具接入：`notion` preset 默认暴露 `search_pages`、`fetch_page` 为 read-only，`slack` preset 默认暴露 `search_messages`、`list_channels` 为 read-only；`create_page`、`post_message` 这类写工具保持确认敏感，不作为个人助理稳定工具的默认后端。

## 暂不新增

- 邮件发送、短信发送、真实下单、真实支付。
- 地图路线和打车/票务类操作。
- 任意浏览器自动化、登录态网页操作。

这些能力权限更重，必须等基础个人事务工具、确认 UX、幂等账本和审计链路稳定后再接入。

## Workflow Skill v1

当前新增三个确定性 workflow manifest：

- `skills/workflows/morning_briefing.json`
- `skills/workflows/schedule_meeting.json`
- `skills/workflows/capture_action_items.json`

manifest 只声明 governed tool steps。写步骤使用 `confirmation: true` 和 `idempotency: "required"`，实际确认事实只来自 runtime metadata；模型参数中的 `confirmed=true` 不构成授权。

## 后续扩展判断

当默认工具数量接近 20 个，或 `ToolRegistry` 注册重复明显增长时，再评估轻量 `ToolRegistryBuilder` / lazy factory descriptor。该扩展必须保留依赖注入语义，不引入模块级全局 registry，也不能绕过现有治理链路。

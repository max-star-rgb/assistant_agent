# 工具重复执行策略统一分类设计

## 背景

当前运行时已经支持两种 `ToolSpec.repeat_policy`：

- `distinct_inputs`：同一 run 内允许使用不同的规范化参数重复调用；已经成功执行过的相同参数不得再次执行。
- `once_per_run`：同一 run 内首次成功后，不再允许再次执行该工具。

现有内置工具大多没有显式声明策略，因此继承 `ToolSpec` 的保守默认值
`once_per_run`。这使 `visual_reminder_manage(action="list")` 成功后，后续
`action="cancel"` 会被误判为重复调用。与此同时，图片生成仍有一条独立于
`ToolSpec.repeat_policy` 的终止型工具去重路径，形成了隐含的第三套规则。

## 目标

1. 所有内置工具都按工具语义明确归入现有两种策略之一。
2. `ToolSpec.repeat_policy` 成为运行时判断工具能否重复执行的唯一策略来源。
3. 所有允许重复的工具仍受 assistant loop 的 `max_tool_iterations` 上限约束。
4. 保留相同成功参数不重复执行、失败后允许修正参数重试的既有语义。
5. MCP 工具根据结构化的只读配置自动映射策略，不依据名称或自然语言关键词判断。

## 非目标

- 不新增第三种重复策略。
- 不修改 Provider tool schema，也不把重复策略暴露给模型。
- 不改变工具授权、安全校验、幂等键、审计或执行链路。
- 不根据用户话术、工具名称或正则表达式推断策略。
- 不为本次分类调整 `max_tool_iterations`。

## 统一契约

### 策略来源

本地内置工具在工具类上显式声明 `repeat_policy`，注册后由 `ToolRegistry` 写入
`ToolSpec`。运行时只读取注册表中的 `ToolSpec.repeat_policy`，不维护工具名称白名单
或特殊分支。

`ToolBase` 保留 `once_per_run` 作为兼容性默认值。这样，尚未迁移的外部插件或旧式
工具仍采用保守策略；本仓库内置工具不得依赖这个隐式默认值。

### 执行语义

| 策略 | 已有成功记录 | 后续调用 |
| --- | --- | --- |
| `distinct_inputs` | 同工具、相同规范化参数 | 拒绝，返回 `duplicate_complete_tool_call` |
| `distinct_inputs` | 同工具、不同规范化参数 | 允许，直到达到 `max_tool_iterations` |
| `once_per_run` | 同工具任意一次成功 | 拒绝，返回 `tool_repeat_limit_reached` |
| 任一策略 | 前次调用失败或被拒绝 | 不计为成功额度，允许后续合法重试 |

决策阶段和执行阶段继续使用同一套通用检查。执行阶段的复核用于防止状态变化、并发
或模型输出绕过决策阶段，不构成另一套策略。

既有失败保护仍然独立生效：失败后可以修正参数重试，但完全相同的失败调用继续由
`duplicate_failed_tool_call` 拦截，避免无进展循环。

## 内置工具分类

### `distinct_inputs`

查询、读取、观察、探索类工具，以及业务上支持一次 run 内执行多个不同动作或创建
多个不同对象的工具，采用 `distinct_inputs`：

| 工具 | 分类依据 |
| --- | --- |
| `weather` | 不同地点或时间范围可连续查询 |
| `calendar_search` | 不同查询条件可连续检索 |
| `contacts_search` | 不同联系人条件可连续检索 |
| `email_search` | 不同条件可连续检索 |
| `email_read` | 可读取不同邮件 |
| `local_file_read` | 可读取不同文件或范围 |
| `web_search` | 可执行不同搜索 |
| `web_fetch` | 可抓取不同页面 |
| `lodging_search` | 可使用不同住宿条件搜索 |
| `shopping_search` | 保留已有策略，可执行不同商品搜索 |
| `visual_image_search` | 可使用不同视觉查询 |
| `media_inspect` | 可检查不同媒体或问题 |
| `live_view_inspect` | 继承媒体检查语义，可执行不同观察 |
| `realtime_video_observe` | 可执行不同实时观察 |
| `VideoUnderstandingBranch`（内部实现，注册名为 `media_inspect`） | 可对不同视频输入或问题分支处理 |
| `visual_memory_search` | 可使用不同条件检索视觉记忆 |
| `load_skill` | 可加载不同 skill |
| `load_skill_reference` | 可加载不同引用资源 |
| `web_page_inspect` | 可检查不同页面或目标 |
| `python_interpreter` | 同一 run 内可能需要不同代码步骤；相同成功代码仍去重 |
| `web_page_explore` | 多步网页任务需要不同探索动作；仍受安全治理和迭代上限约束 |
| `visual_reminder_manage` | 支持 `list`、`create`、`cancel` 等不同动作及多个不同提醒 |
| `calendar_create` | 允许创建多个不同日程；相同输入仍由重复检查和幂等机制保护 |
| `hotel_price_watch_create` | 允许创建多个不同监控任务 |

### `once_per_run`

同一 run 内再次成功执行通常表示重复提交，或结果本身已支持批量产出的终止型工具，
采用 `once_per_run`：

| 工具 | 分类依据 |
| --- | --- |
| `task_plan_submit` | 一次 run 只提交一个最终任务计划 |
| `image_generation` | 单次请求已支持批量生成，避免模型再次触发高成本生成 |
| `image_to_3d` | 避免同一 run 重复触发高成本 3D 生成 |

## MCP 工具映射

MCP 工具在代理工具及生成的 `ToolSpec` 中使用已有结构化配置
`is_read_only` 映射策略：

- `is_read_only=true` -> `distinct_inputs`
- `is_read_only=false` -> `once_per_run`

该映射同时应用于 MCP proxy 注册和 definition-to-`ToolSpec` 适配路径，避免同一 MCP
工具因入口不同获得不同策略。未提供可信只读声明时继续按写工具处理，使用
`once_per_run`。

## 运行时收敛

删除图片生成专用的 `terminal_tools` 成功记录与重复拦截。`image_generation` 改为通过
显式 `repeat_policy="once_per_run"` 获得完全相同的防重复效果。完成后，运行时不再按
工具名称决定重复执行行为。

通用保护保持不变：

1. assistant loop 的决策阶段在准备调用前检查策略。
2. 执行边界在真实调用前再次检查策略。
3. 只有结构化结果为成功的调用才写入成功记录。
4. 所有调用继续受 `max_tool_iterations`、`ActionValidator -> ToolExecutor -> ToolRegistry`
   以及各工具自身授权和幂等规则约束。

## 可观测性与兼容性

- `once_per_run` 的重复调用继续记录并返回 `tool_repeat_limit_reached`。
- `distinct_inputs` 的相同成功参数继续记录并返回
  `duplicate_complete_tool_call`。
- 事件字段沿用现有工具名、规范化参数、状态和拒绝原因，不新增协议字段。
- 旧外部插件不声明策略时仍安全地退化为 `once_per_run`。
- 本次变更不影响 Provider-native 只读联网能力，因为它不属于本地 Tool 执行链。

## 验证范围

使用现有 `tests/tdd/tool-repeat-policy/` 临时 RED/GREEN 测试覆盖：

1. 每个内置工具都显式声明且分类与本设计一致。
2. `visual_reminder_manage` 的 `list -> cancel`、不同 `create` 可连续执行，相同成功参数
   会被拒绝。
3. `calendar_create` 和 `hotel_price_watch_create` 的不同创建允许连续执行。
4. 三个 `once_per_run` 工具在首次成功后拒绝第二次不同参数调用。
5. MCP 只读/写工具在两条注册路径中分别映射到正确策略。
6. 图片生成不再依赖专用终止型工具分支，通用策略仍能阻止第二次执行。
7. 失败调用不消耗成功额度，迭代上限仍然生效。

根据 `tests/README.md` 复核是否存在需要晋升为永久 core invariant 的规则。本次首先
作为现有工具重复策略 feature 的 TDD 覆盖；除非实现过程中发现通用治理 invariant
缺口，不机械新增永久测试。

## 文档同步

实现完成后，在 `docs/tool-calling-architecture.md` 中同步：

- 两种策略的统一语义；
- 内置工具分类原则和 MCP 映射；
- `ToolSpec.repeat_policy` 是唯一重复执行策略来源；
- `ToolBase` 默认值只承担外部兼容和安全回退。

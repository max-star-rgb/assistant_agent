# 📎 补充信息：工具调用设计 - 详情

---

## 🟡 原始追问

我想知道本项目中工具调用产生了哪些字段？哪些是特有的，哪些是通用的。

---

## Q1. 工具调用的 4 层抽象与特有字段设计 🟡

### 【我的补充提问】
我想知道本项目中工具调用产生了哪些字段？哪些是特有的，哪些是通用的。

---

### 【面试官解答】

#### 4 层抽象的边界

```
ToolSpec (工具是什么)
    ↓
ToolSelection (Agent 选了什么工具)
    ↓
ToolResult (工具执行的原始输出 → Runtime 内部用)
    ↓
ToolObservation (给 LLM 看的裁剪后观察结果 → 进入 prompt)
    ↓
ToolCallRecord (持久化记录 → 存数据库/日志)
```

#### 各层字段对比表

| 层级 | 字段 | 通用/特有 | 说明 |
|-----|------|----------|------|
| **ToolSpec**<br>（工具定义） | `name` | 通用 | 工具名称 |
| | `description` | 通用 | 工具描述 |
| | `input_schema` | 通用 | JSON Schema 输入定义 |
| | `required_inputs` | 通用 | 必填字段列表 |
| | `when_to_use` | 特有 | **本项目独创**：告诉 LLM 什么时候用这个工具 |
| | `when_not_to_use` | 特有 | **本项目独创**：告诉 LLM 什么时候**不要**用这个工具 |
| | `runtime_constraints` | 特有 | **本项目独创**：运行时约束 |
| | | | |
| **ToolSelection**<br>（Agent 选择） | `tool_name` | 通用 | 选中的工具名 |
| | `reason` | 特有 | **本项目独创**：Agent 选择这个工具的理由（CoT 的一部分） |
| | `input` | 通用 | 工具入参 |
| | `step_id` | 通用 | 步骤 ID |
| | | | |
| **ToolResult**<br>（原始执行结果）<br>Runtime 内部用 | `tool_name` | 通用 | 工具名 |
| | `success` | 通用 | 是否成功 |
| | `data` | 通用 | 原始输出数据（可能很大） |
| | `error` | 通用 | 错误信息 |
| | `output_ref` | 特有 | **本项目独创**：大输出的引用（artifact://xxx），不直接存数据 |
| | `latency_ms` | 通用 | 执行耗时 |
| | `contract` | 特有 | **本项目独创**：输出契约（用于校验 data 的格式） |
| | | | |
| **ToolObservation**<br>（给 LLM 看的）<br>进入 prompt | `tool_name` | 通用 | 工具名 |
| | `status` | 通用 | `succeeded` / `failed` / `rejected`（多了 rejected 状态） |
| | `summary` | 特有 | **本项目独创**：自动生成的自然语言摘要，LLM 直接读这个 |
| | `output_ref` | 通用 | 同 ToolResult |
| | `structured_output` | 特有 | **裁剪后的**结构化输出（白名单字段保留） |
| | `error_code` | 通用 | 错误码 |
| | `error_message` | 通用 | 脱敏后的错误信息 |
| | `next_step_hint` | 特有 | **本项目独创**：给 LLM 的下一步提示 |
| | `redacted` | 特有 | **本项目独创**：标记是否已做脱敏裁剪 |
| | | | |
| **ToolCallRecord**<br>（持久化记录） | `call_id` | 通用 | 调用 ID |
| | `tool_name` | 通用 | 工具名 |
| | `input` | 通用 | 入参 |
| | `status` | 通用 | 状态 |
| | `started_at` / `finished_at` | 通用 | 时间戳 |
| | `output_ref` | 通用 | 输出引用 |
| | `error_message` | 通用 | 错误信息 |

#### 本项目特有设计的精华

| 特有字段 | 设计意图 |
|---------|---------|
| **`ToolSpec.when_to_use` / `when_not_to_use`** | 不是让 LLM 自己猜"什么时候用这个工具"，而是把最佳实践写死在 spec 里，大幅减少选错工具的概率 |
| **`ToolObservation.summary` + `next_step_hint`** | 把"工具返回了什么"和"接下来应该做什么"直接喂给 LLM，减少 LLM 的推理负担，提升准确率 |
| **`output_ref` 引用机制** | 大输出（图片、长文本）不直接进 prompt，而是存 artifact，LLM 需要时用 `get_artifact` 主动取，既省 token 又防泄漏 |

---

### 【本项目代码位置】

- `src/multimodal_agent/schemas/tools.py` - 工具各层 schema 定义
- `src/multimodal_agent/schemas/tool_observation.py` - ToolObservation 实现

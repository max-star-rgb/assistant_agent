# 原生公开输出、评测与 Tool 交付统一设计

日期：2026-09-02
状态：已批准，进入实施

## 1. 文档定位

本文统一生产 `AssistantAgent` 的公开输出、LangSmith Experiment 输入、长期 Memory 私有状态和 Tool 结果确定性交付。
它是开发规格，不替代 `docs/*.md` authority；实施完成后必须同步更新对应 authority。

本设计修正的不是 LangGraph 缺失能力，而是项目对原生边界的使用不一致：内部 Memory state 被公开到根 Run
Outputs；evaluation target 丢弃了部分公开 Graph 结果；媒体终态又按具体 Tool 名重新解析业务 artifact。

## 2. 已确认目标

- 生产 Graph 继续由 `create_deep_agent` 直接编译，不增加 wrapper graph。
- 标准 `messages` 是对话、Tool 调用和 Tool 结果的唯一事实源。
- Experiment 保留完整公开 Graph 结果，以支持最终回答、trajectory、Tool 和 single-step 评测。
- single-step 的 node/LLM/Tool 细节来自原生 Trace，不复制到 Graph Outputs。
- Tool 使用原生 `content_and_artifact -> ToolMessage` 声明必须交付给用户的结果。
- Runtime 只执行通用交付规则，不识别购物、酒店、高德或图片生成等业务 Tool 名。
- 只阻止未来 Memory 污染；历史 Memory 先只读审计，未经确认不删除。

## 3. 明确非目标

- 不新增顶层 `final_response`、`deliveries` 或第二套 state channel。
- 不把完整 Trace、child spans 或 Provider payload 展平到 Experiment Outputs。
- 不依赖最终 LLM 回复复述链接、图片或下载地址。
- 不在本次迁移中修改真实 LangSmith evaluator、清理历史 Memory 或调用真实 Provider。
- 不重写 LangGraph、Deep Agents、LangMem、MCP adapter 或 Agent Server 生命周期。

## 4. 统一状态与公开输出

`AssistantAgentState` 继续扩展 `DeepAgentState`。原生 schema metadata 决定字段可见性：

```text
公开 Graph output
├─ messages                  必需，完整 Human/AI/ToolMessage 序列
├─ todos                     Deep Agents 原生可选
└─ structured_response       LangChain 原生可选

内部 state/checkpoint
├─ memory_context
├─ memory_status
├─ needs_verification
├─ verification_attempts
├─ provider_search_profile
└─ async_tasks
```

项目自定义内部字段使用官方 schema metadata，而不是只用 `OmitFromInput`。不进入 subagent 的字段用
`PrivateStateAttr`；`memory_context` 需继续经显式 allowlist 传给 `general-purpose` worker，因此组合
`OmitFromInput + OmitFromOutput`。这不会删除 channel，也不会阻止 middleware、Tool、checkpoint 或 resume
使用字段，只会从原生公开 input/output schema 中排除它们。

`messages` 不做二次裁剪。Tool trajectory 由 `AIMessage.tool_calls` 与对应 `ToolMessage.tool_call_id` 表达；最终回答由
最后一个非空 `AIMessage` 表达。

## 5. LangSmith Experiment 契约

`NativeGraphEvaluationTarget` 直接调用生产 Graph，并保存其完整公开返回值，不再只复制 `messages`。评测结果对象提供：

- `output`：完整公开 Graph result；
- `messages`：从 `output["messages"]` 派生的标准消息序列；
- `response_message/response_text`：只作为 evaluator 便利访问器，不写回 Graph output。

Experiment 和 evaluator 的职责分开：

| 评测类型 | 权威事实 |
| --- | --- |
| 最终回答正确性 | `output.messages` 中最后一个 `AIMessage` 的标准文本块 |
| trajectory | 完整 `output.messages` |
| Tool 选择与结果 | `AIMessage.tool_calls`、`ToolMessage.name/tool_call_id/status/artifact` |
| single-step | LangSmith 原生 Graph/node/LLM/Tool child runs |
| latency/token/error | 原生 Trace 字段 |

当前线上 `Correctness` 的 `var1=output.messages[-1].content` 会得到 content block 列表而非稳定纯文本；本次只在实验计划中
记录修正目标，不远程更新 evaluator。未来正式 Experiment runner 应使用评测对象的 `response_text` 构造 judge 输入，同时让
Experiment Outputs 继续保留完整 `output`。

## 6. 统一 Tool delivery artifact

Tool 仍返回自身领域 artifact，不创建通用领域结果。需要确定性交付时，在 artifact 内增加唯一 namespaced 字段：

```json
{
  "assistant_agent_delivery_v1": {
    "text": "可选的最终用户可见文本或卡片协议块",
    "output_refs": ["可选的受管生成物引用"]
  }
}
```

该字段由一个严格 Pydantic model 校验：禁止额外字段，文本和引用数量/长度有界，空 delivery 不成立。它只是原生
`ToolMessage.artifact` 中的应用数据，不是新 state、消息类型或执行协议。

内建 Tool 在已经完成领域 schema、URL、路径与媒体安全校验后构造 delivery：

- shopping：Tool 根据已选择商品生成有界 `<detail>`；
- lodging：Tool 根据已验证报价生成有界 `<detail>`；
- image generation：Tool 写入已物化、已验证的受管 `output_refs`；
- AMap MCP：interceptor 将相同 delivery 写入 `structuredContent`，官方 adapter 转换后位于
  `artifact.structured_content.assistant_agent_delivery_v1`；
- 标准 `type=file` content block：它本身就是原生交付声明，无需重复写 artifact delivery。

Runtime 的通用算法：

1. 只看最后一个 HumanMessage 之后的当前轮消息；
2. 按 `ToolMessage.name` 保留最后一次调用；
3. 最后一次非 success 时不回退同名旧结果；
4. 从 artifact 顶层或 MCP `structured_content` 的统一 key 读取 delivery；
5. 同时读取安全 HTTP(S) `type=file` block；
6. 按消息顺序去重、合并有界文本和 `output_refs`。

Runtime 不导入任何 shopping/lodging/image/AMap 领域 model 或 Tool ID。异步任务仍使用既有 Store/outbox/callback，不借
delivery artifact 建立第二套异步生命周期。

## 7. Memory 原生治理

`memory_context/status` 私有化只解决公开输出污染，不解决 Store 中的低质量记忆。未来写入继续由独立
`assistant-memory-v1` 与 LangMem manager 完成，不在主图增加判断节点。

LangMem manager 改为使用 Pydantic `DurableMemory` schema，明确允许的类型只有稳定事实、偏好、长期目标和可复用流程；
正文继续放在 `content` 字段，以兼容当前 recall。manager instruction 明确：没有长期价值时必须不产生 insert，禁止把
“一次性查询”“无需保存”等分类理由作为记忆正文。recall 继续兼容历史 string 与新的结构化记录。

该 schema 是 LangMem 官方 `create_memory_store_manager(schemas=...)` 能力；不新增关键词过滤器、二次 LLM、Memory
Registry 或自研 extraction graph。历史污染记录不自动删除。

## 8. 错误与安全

- 无效 delivery 整体忽略，不使成功 Graph run 变成 transport error。
- Tool 领域数据仍先经过现有 Pydantic/URL/路径校验；Runtime 不重新解释业务字段。
- 标准 file block 只接受无控制字符、不会突破 Markdown 边界的 HTTP(S) URL。
- `output_refs` 只由受信内建 Tool 生成，媒体发送前仍经过现有受管 artifact 解析。
- MCP 外部服务不能直接声明 delivery；全局 interceptor 先删除外部结果中的 namespaced key，仅对服务端校验过的
  AMap 路线重建 URL 和 delivery。
- Memory 正文不再进入根 Graph Outputs；是否记录内部 node state 继续服从 LangSmith 部署脱敏策略。

## 9. 迁移与删除

迁移顺序：state 私有化和 evaluation result；统一 delivery schema；四类 Tool/MCP 写入；Runtime 通用消费；Memory schema；
authority 与测试收口。

完成后删除业务感知的 `agent_server/shopping_detail.py`，并从 `turn_delivery.py` 删除所有领域 import、Tool 名常量和业务
artifact 解析。旧 artifact 的兼容读取不保留；同一次发布同时迁移生产 Tool writer 和 Runtime reader，避免双轨。

## 10. 验收标准

- 编译后的生产 output schema 不包含项目自定义内部字段，checkpoint 内仍可读取它们。
- `NativeGraphEvaluationTarget.output` 等于生产 Graph 的完整公开返回值，最终回答/trajectory/Tool/single-step 均有权威读取路径。
- 购物、酒店、导航、文件和生成图片在最终 AIMessage 不含链接时仍可确定性交付。
- `turn_delivery.py` 不识别任何业务 Tool 名，也不导入任何领域 model。
- 同名 Tool 的最后一次失败不会回退旧 delivery；畸形 delivery/file URL 不泄漏且不抛出。
- LangMem manager 使用官方结构化 schema，并继续兼容历史 recall。
- Memory、Tool、Runtime、媒体、evaluation authority 与实现一致。
- 默认 mock/offline 验证、完整 `tests/core`、相关临时 TDD、Ruff、authority validator 和 8089 hot reload 全部通过。
- 不调用真实 Provider，不修改远端 evaluator，不删除历史 Memory。

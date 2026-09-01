# Qwen VLM 原生 BaseChatModel 改造设计

日期：2026-09-01

状态：已确认，待实施

本文是开发阶段设计材料，不是当前生产 authority。实现完成后，以同步更新的
`docs/*.md` authority、源码和测试为准。

## 1. 背景

当前主 Graph 的 Qwen LLM 由 `DashScopeNativeChatModel(BaseChatModel)` 调用，LangChain callback
自动创建标准 LangSmith model run。Qwen 图片理解虽然已经改用百炼原生
`multimodal-generation` HTTP API，但仍经过 `DashScopeVisionProviderAdapter` 的手工 `urllib`
调用，并由 `observe_vision_inference()` 手工创建 `vlm.infer` span。

这导致两类问题：

- `vlm.infer` 只记录上层业务 question，adapter 实际追加的最终 prompt 不会自动进入 trace；
- 输入、输出、Provider、model 和 usage 不是 LangChain model callback 的标准结构，和主 Graph LLM
  形成两套观测机制。

本次改造覆盖所有 Qwen 图片理解，包括后台关键帧窗口和用户上传图片。显式视频 Provider、非 Qwen
Vision Provider、SigLIP2、选帧算法和主 Graph 不在本次迁移范围内。

## 2. 目标与非目标

### 2.1 目标

- 所有 Qwen 图片理解统一通过 `BaseChatModel.invoke/ainvoke` 调用。
- 使用百炼原生 `multimodal-generation` endpoint，不退回 Realtime 或 OpenAI-compatible API。
- `vlm.infer` 由 LangChain callback 自动创建，行为与主 Graph LLM run 一致。
- LangSmith 中自动展示实际发送的最终 prompt、有序多图消息、Provider、model、latency 和 token usage。
- 默认输出预览只显示 `summary`；详情保留完整结构化 JSON，包括 `objects`、`colors` 等业务字段。
- 后台 `vision.observation` 与 AssistantAgent 保持独立 root，通过可信 `thread_id` 关联；视觉窗口继续并行。
- 保留现有 `VisualUnderstandingResult`，不削弱上传图片和视觉时间线依赖的结构化字段。

### 2.2 非目标

- 不把后台视觉流水线改成 LangGraph node。
- 不复用主 Agent 实例、对话 state、memory、Tool 或 checkpoint。
- 不引入 DashScope Python SDK；当前原生 HTTP transport 已满足 Provider 协议。
- 不迁移 Ark、OpenAI 或 mock Vision adapter 到 `BaseChatModel`。
- 不恢复 Realtime 音频静音或 Provider 会话时序方案。
- 不新增自研 trace store、模型调用框架或第二套 callback 系统。

## 3. 方案选择

采用扩展现有 `DashScopeNativeChatModel` 的方案，使其同时支持 text-generation 与
multimodal-generation：

- 主 Graph 继续使用默认 text 模式，现有行为不变；
- Qwen Vision adapter 使用 multimodal 模式，发送 LangChain 标准多模态消息；
- 两种模式共用 HTTP transport、错误映射、response metadata、usage 解析和 LangChain callback。

不采用以下方案：

- `ChatOpenAI`/OpenAI-compatible：虽然 callback 接入更直接，但放弃已验证的百炼原生有序多图接口；
- 继续增强手工 `trace()`：改动更小，但不能消除主 LLM 与 VLM 的双轨观测；
- 新建一套独立 Vision ChatModel：会复制现有 DashScope transport、usage 和错误处理。

## 4. 组件与职责

### 4.1 `DashScopeNativeChatModel`

在现有类上增加窄的调用模式，默认值保持 text：

```text
mode=text
  -> /api/v1/services/aigc/text-generation/generation
  -> 现有文本与 Tool message serializer

mode=multimodal
  -> /api/v1/services/aigc/multimodal-generation/generation
  -> 保留 LangChain content blocks 的图片顺序
```

multimodal serializer 接受标准 `HumanMessage.content`：

```python
[
    {"type": "image", "base64": "...", "mime_type": "image/jpeg"},
    {"type": "image", "base64": "...", "mime_type": "image/jpeg"},
    {"type": "text", "text": "<最终完整 prompt>"},
]
```

发送到 DashScope 时转换为同序的 `{"image": data_url}` 和末尾 `{"text": prompt}`。不得排序、反转、
并行构造或拆成多次 Provider 请求。参数固定保留 `enable_thinking=false`、`result_format=message` 和
`temperature=0`。

模型响应继续映射为 `AIMessage`，并填写：

- `response_metadata.model_name/provider/api_protocol/finish_reason/provider_request_id`；
- `usage_metadata.input_tokens/output_tokens/total_tokens`；
- `content` 为结构化结果中的 `summary`，供 LangSmith 默认预览；
- 完整解析后的 Provider JSON 放入 `additional_kwargs["structured_output"]`，供详情和业务 adapter 使用。

原始 Provider response、凭据和请求头不得进入 trace。

### 4.2 `DashScopeVisionProviderAdapter`

adapter 不再拥有 HTTP 调用，只负责：

1. 按 `image_ids` 原顺序读取图片并构造 LangChain image blocks；
2. 由同一个 prompt resolver 生成最终完整 prompt；
3. 调用 multimodal `DashScopeNativeChatModel.invoke/ainvoke`；
4. 从 `AIMessage.additional_kwargs["structured_output"]` 映射为现有
   `VisualUnderstandingResult`。

prompt resolver 是单一事实来源；trace 中的文本与 DashScope 实际收到的文本必须来自同一个字符串，禁止
在 observation service 和 adapter 各维护一份 schema 后缀。

### 4.3 `VisionUnderstandingClient`

统一 client 继续决定 image/video adapter，不承担模型推理。Qwen image adapter 接收一个窄的
`RunnableConfig`，用于传递 `run_name`、预生成 `run_id`、tags 和 metadata。非 Qwen adapter 继续使用现有
手工 tracing fallback，避免本次改造扩大到其他 Provider。

不得把 LangSmith API key、client 或 RunTree 放入业务 request model。

## 5. 调用与 trace 数据流

### 5.1 后台关键帧窗口

```text
vision.observation (LangSmith root, parent=ignore)
  -> DashScopeNativeChatModel.invoke
       run_name=vlm.infer
       run_id=<预生成 UUID>
       messages=[ordered images..., final prompt]
  -> parse structured_output
  -> VisualSemanticRecord
```

`vision.observation` 继续记录 window ID、sequence、timestamps、role 和 semantic threshold，并保留
`selected-keyframes-video` MP4 attachment。各窗口继续拥有独立 asyncio task 和 model/client 实例，互不等待。

有序 JPEG 改由 `vlm.infer.inputs.messages` 展示，不再同时作为 root JPEG attachments 上传，避免同一图片在
一个 trace 中存储两份。root 的 MP4 仍用于快速回放关键帧选取过程。

预生成的 `vlm.infer` run ID 在调用前写入 `VisionInferenceTraceLink`，使
`source_vlm_span_id`、target barrier 和视觉时间线继续精确关联真实 model run。LangChain callback 必须在当前
`vision.observation` tracing context 内执行，使 child 自动继承 parent。

### 5.2 用户上传图片

```text
AssistantAgent root
  -> ToolNode / uploaded_media_inspect
       -> DashScopeNativeChatModel.invoke(run_name=vlm.infer)
       -> parse structured_output
```

上传图片复用完全相同的 Qwen model 与 prompt resolver。它不创建独立
`vision.observation` root；`vlm.infer` 直接成为当前 Tool run 的原生后代。

## 6. LangSmith 展示契约

`vlm.infer` 必须是 `run_type=llm`，并使用 LangChain 标准 messages/output：

- Inputs：有序图片 content blocks 和最终完整 prompt；
- Output preview：`AIMessage.content == summary`；
- Output details：`additional_kwargs.structured_output` 中保留完整 JSON；
- Metadata：LangChain model identifying params 自动提供 Qwen Provider、model 与 DashScope protocol；
- Usage：来自 DashScope response 的标准 `usage_metadata`；
- Run name：固定 `vlm.infer`。

不再由 Qwen 路径的 `_VLM_OUTPUT_FIELDS` 手工决定 model output。现有手工
`observe_vision_inference()` 只保留给尚未迁移为 `BaseChatModel` 的 Provider，不得包裹 Qwen model run 形成
重复的 `vlm.infer -> model` 两层 LLM span。

## 7. 错误与降级

- DashScope HTTP、timeout、bad response 和结构化 JSON 解析失败继续映射为脱敏的
  `ProviderAdapterError`；不得记录原始 response。
- model run 失败时由 LangChain callback 正常结束为 error，业务层仍返回现有结构化 unavailable/error。
- tracing 上传失败必须 fail-open，不得重复调用 Provider。
- real mode 配置不完整时继续 fail closed，不回退 mock。
- mock mode 不联网；mock Vision 可继续使用现有手工 span，以保护离线测试。
- Qwen multimodal 初始化或调用失败不得回退 Realtime adapter、OpenAI-compatible endpoint 或旧 HTTP adapter。

## 8. 迁移与删除

实现完成后删除 Qwen 图片路径中的以下重复逻辑：

- `DashScopeVisionProviderAdapter` 自有的 `urllib.request.Request/urlopen`；
- 与 `DashScopeNativeChatModel` 重复的 response usage、HTTP error 和 metadata 解析；
- Qwen 调用外围的手工 `vlm.infer` LLM span；
- root 上重复的 JPEG attachments。

保留 Provider-neutral schema 映射、非 Qwen tracing fallback、MP4 生成和视觉 root 生命周期。迁移不得删除
旧 Realtime adapter 的显式视频兼容能力，除非后续独立任务证明无调用者并批准删除。

## 9. 验证

离线验证至少覆盖：

- text 模式的主 Graph Qwen payload 与现有行为不变；
- multimodal endpoint 选择正确；
- 1～5 张图片的 LangChain blocks 与 DashScope payload 顺序一致，prompt 位于最后；
- `enable_thinking=false`、`temperature=0`；
- `AIMessage.content` 只为 summary，完整结构在 details；
- DashScope usage 正确映射；
- Qwen 路径只产生一个原生 `vlm.infer` model run；
- 后台 run 是 `vision.observation` child，上传图片 run 是 Tool child；
- `source_vlm_span_id` 等于真实 model run ID；
- 非 Qwen 和 mock fallback 不回归；
- root 保留 MP4、JPEG 只在 `vlm.infer` 原生 messages 中出现。

真实验证必须由 operator 显式启用 real Provider，使用同一组“键盘、水杯、水杯”有序关键帧检查：

- 最后一帧识别为水杯；
- `vlm.infer` 展示最终完整 prompt 和三张有序图片；
- 默认 output preview 为 summary；
- details 可展开完整结构化 JSON；
- Provider、model、latency、usage 和 parent run 正确；
- 一次窗口只发生一次 DashScope Provider 请求。

真实媒体、Provider response 和凭据不写入仓库或 artifact。实现完成后同步更新
`docs/visual-perception-architecture.md` 与 `docs/observability-harness.md`。

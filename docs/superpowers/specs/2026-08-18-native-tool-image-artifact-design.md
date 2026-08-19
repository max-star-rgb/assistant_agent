# 生图 Tool 原生 content/artifact 双通道设计

日期：2026-08-18

## 目标

使用 LangChain `response_format="content_and_artifact"` 的原生双通道表达生图结果：

- `ToolMessage.content` 只保存给主模型的成功文本和 `image_id`；
- `ToolMessage.artifact` 保存程序使用的受管图片引用、可访问 URL 与补充运行元数据。

Graph、模型、普通 Agent Server 客户端和 Media WebSocket 不再依赖最终 `AIMessage` 的图片投影。删除
`project_generated_images` 父图节点，不为当前 Studio Chat 增加 Markdown 或其他显示兼容。

## 官方语义

LangChain 的 `ToolMessage.content` 是 Tool 执行后发送给模型的结果；`ToolMessage.artifact` 是不会发送给模型、
供程序访问的补充数据。`BaseTool(response_format="content_and_artifact")` 返回二元组后，ToolNode 会把二者写入
同一个 ToolMessage。

本项目 `ToolBase` 已采用这一原生机制：`model_observation` 成为 `content`，完整 `ToolResult.data` 成为
`artifact`。因此无需创建两个 ToolMessage、无需增加 model-call middleware，也无需把图片 content block
放入模型历史。

## 消息契约

生图成功后的消息形态为：

```json
{
  "type": "tool",
  "name": "image_generation",
  "tool_call_id": "call-123",
  "status": "success",
  "content": "{\"image_id\":[\"349cc6\"]}",
  "artifact": {
    "status": "succeeded",
    "images": [
      {
        "image_id": "349cc6",
        "output_ref": "/artifacts/generated/349cc6.png",
        "url": "http://127.0.0.1:8089/artifacts/generated/349cc6.png",
        "mime_type": "image/png"
      }
    ],
    "provider_image_urls": [
      "https://provider.example/generated.png"
    ],
    "request_id": "request-123"
  }
}
```

字段职责：

- `content`：ToolNode 将窄 `model_observation` 序列化得到的字符串，只向主模型提供成功状态和
  `image_id`；模型不接收 URL、Base64 或图片正文；
- `artifact.images[]`：每张图片的单一结构化描述，避免多个平行数组依赖索引对齐；
- `artifact.images[].output_ref`：服务端受管的稳定相对引用，供本地读取、WS 投影和 3D 复用；
- `artifact.images[].url`：由可信 `artifact_base_url` 与受管引用确定性构造的客户端可访问绝对 URL；
- `artifact.images[].image_id/mime_type`：稳定图片标识与声明 MIME；读取文件时仍以实际字节校验结果为准；
- `artifact.provider_image_urls`：Provider 原始返回，只用于诊断和审计，不作为客户端交付地址；
- 其余 task/request/provider/model 字段继续作为补充运行元数据。

约束：

- 只有成功物化到受管 `/artifacts/generated/*` 的文件才能产生客户端 image URL；
- URL 由服务端配置构造，模型不参与拼接；
- 没有 `artifact_base_url` 时保留 `images[].output_ref`，省略该项的 `url`，不伪造 origin；
- 多图片按 Provider 顺序输出，去重并沿用最多四张的现有交付上限；
- 失败 ToolMessage 只包含结构化错误，不产生受管引用或客户端 URL；
- Provider URL、受管引用和文件路径不进入 `ToolMessage.content`。

## Graph 与数据流

父图恢复为：

```text
capture_trusted_runtime_facts
  -> memory_recall
  -> execution_router
       fast     -> AssistantFastAgent --------+
       planning -> AssistantPlanningGraph     |
  -> refresh_memory_extraction <--------------+
  -> END
```

删除 `project_generated_images` 节点、`project_generated_images_node` 和最终 AIMessage 改写逻辑。最终
AIMessage 只包含模型生成的文本回答。

执行与消费流程：

```text
image_generation
  -> ToolMessage
       content  -> LLM：成功状态 / image_id
       artifact -> 普通客户端：images[].url
                -> Media WS：images[].output_ref -> 本地文件 -> Base64 IMAGE detail
                -> image_to_3d：受管 output_ref / image_id
                -> Tracing：完整结构化 Tool 结果
```

各入口只读取 terminal state 中最后一个 HumanMessage 之后、成功的 `image_generation` ToolMessage artifact，
不聚合旧轮次，不从最终 AIMessage 或模型自然语言中猜测图片。

## 入口行为

### 普通 Agent Server 客户端

读取 `ToolMessage.artifact.images[]`，使用其中的 `url` 展示或下载图片。客户端需要图片 ID 或内部关联时读取同一
对象的 `image_id`，不得使用 Provider URL。

### Media WebSocket

读取 `ToolMessage.artifact.images[].output_ref`，执行现有受管路径、文件大小和 MIME 校验，读取本地文件后继续投影为
既有 `intentResult.detail[].type=IMAGE` Base64 正文。WS wire schema、ACK 和 delivery ID 不变。

### Studio 与 Tracing

当前 Studio Chat 不读取 ToolMessage artifact，因此不显示图片，这是本设计接受的限制。Tracing 保留完整 artifact
数据；LangSmith UI 是否把其中的 URL 渲染为图片预览属于 UI 能力，不作为后端协议保证。

### 模型

主模型只接收 `ToolMessage.content`。DeepSeek、百炼 Qwen 等要求 tool-role content 为字符串的 Provider 继续使用
现有 LangChain/Provider 序列化路径；artifact 不进入请求，因此不需要额外的消息过滤 middleware。

## 配置与实现范围

- `artifact_base_url` 保持可信部署配置，负责将受管相对引用转换为绝对客户端 URL；
- `ImageGenerationTool` 在构造 `ToolResult.data` 时写入结构化 `images[]`；
- `ToolResult`、`ToolBase` 和 `response_format="content_and_artifact"` 保持现有契约，不新增平行字段；
- `image_generation` 的 `model_observation` 保持窄内容，不加入 URL；
- `src/assistant_agent/native_agent/root_graph.py` 删除图片投影节点与 `artifact_base_url` 参数；
- `src/assistant_agent/native_agent/generated_images.py` 删除；当前轮次 artifact 提取 helper 迁入
  `src/assistant_agent/runtime/generated_artifacts.py`；
- `src/assistant_agent/agent_server/media_app.py` 继续复用该 helper，从 artifact `images[]` 获取受管引用；
- `src/assistant_agent/agent_server/services.py` 不再把 artifact base URL 传给父图；Image Tool plugin 从现有
  `ProviderConfig` 获取该配置；
- 同步更新 runtime、media protocol、Tool 和配置相关 authority 文档。

## 安全、错误与兼容

- 受管引用只允许固定前缀下的单层文件名，拒绝 scheme、netloc、query、fragment 和路径穿越；
- 客户端 URL 只消费可信 `artifact_base_url`，不读取请求 Host、普通 metadata 或转发头推断 origin；
- 图片读取继续受最大字节数、最多四张和 MIME allowlist 约束；
- 旧 checkpoint 中只有 `output_ref/download_urls` 的 artifact 仍可由兼容提取 helper 读取；新 run 统一写入
  `images[]`，迁移期结束前不删除旧字段；
- artifact 缺失、状态失败或引用非法时，入口不投递图片，但文本回答仍可完成；
- 不把 Provider 原始 URL 回退为受管 URL，不静默读取任意公网地址；
- 不调用真实 Provider 验证本次结构变更。

## 验证策略

使用独立 `tests/tdd/native-tool-image-artifact/` 做 RED/GREEN，覆盖：

1. 生图成功时 content 只包含 `image_id`，artifact 包含受管 refs 与绝对 image URLs；
2. scripted model 收到的 ToolMessage 不含 URL、Base64、文件路径或图片 block；
3. 无 base URL 时省略客户端 URL但保留受管 refs；
4. 多图片按顺序去重并受数量上限约束；
5. 失败和非法引用不产生客户端 URL；
6. WS 从 artifact refs 生成与现有一致的 Base64 IMAGE detail；
7. 父图不再包含 `project_generated_images`，最终 AIMessage 不被改写；
8. 旧 `output_ref/download_urls` artifact 兼容读取。

现有 LOOP-001 测试登记了父图稳定拓扑，因此更新其已有节点集合断言；不新增 core invariant。其余 feature 行为
保留在临时 TDD 目录。验证全程使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不访问真实图片 Provider、主模型或
外部服务。

## 非目标

- 不让当前 Studio Chat 展示图片；
- 不向 AIMessage 或 ToolMessage content 添加 image block；
- 不增加 Markdown、Generative UI 或自定义 React UI；
- 不增加 model-call 消息过滤 middleware；
- 不改变图片 Provider、生成参数、存储目录、媒体 WS wire 或 3D callback；
- 不建立独立 artifact state channel、第二套产品终态或入口分发 node。

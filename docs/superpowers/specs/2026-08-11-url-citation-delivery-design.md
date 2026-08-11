# 联网来源角标交付设计

## 目标

把 DashScope 已返回的正文角标与 `search_info.search_results` 转换为 Provider-neutral 的结构化引用，先通过 HTTP `/agent/run` 交付，再由同一 Runtime 契约投影到 Gateway `run.end`。客户端负责把正文中的 `[1]`、`[ref_1]` 渲染为可点击角标；服务端不改写正文为 Markdown，也不追加底部来源列表。

## 范围

- 本轮包含 DashScope `ChatResult.search_sources` 到 `AgentResponse`、`AgentRunResponse`、`RealtimeAgentResult` 和 Gateway `run.end.payload.annotations` 的终态链路。
- 本轮不修改 token streaming；引用在终态 enrichment 阶段一次性交付。
- 本轮不修改 Media-Agent `/agent-service/v1` vendor wire contract，也不实现 Web UI。Media-Agent 后续必须通过 capability negotiation 单独扩展。
- 显式 OpenAI-compatible 回退没有结构化 `search_info` 时返回空 annotations，不猜测来源。

## 公共契约

响应正文保持原样，新增 `annotations`：

```json
{
  "response_text": "推荐入住某酒店 [1]。",
  "annotations": [
    {
      "type": "url_citation",
      "start_index": 8,
      "end_index": 11,
      "source_id": "source_1",
      "title": "酒店详情",
      "url": "https://example.com/hotel"
    }
  ]
}
```

- `start_index` / `end_index` 使用 Python 字符串的 Unicode code point 半开区间，且 `response_text[start_index:end_index]` 必须等于原始角标文本。
- `source_id` 由 Provider source index 形成 `source_<index>`，仅用于一次响应内关联，不作为持久身份。
- 每次角标出现对应一个 annotation；重复引用复用同一个 `source_id`。
- 只投影正文实际引用且能匹配来源的条目。未引用来源、无匹配角标和非 HTTP(S) URL 不进入产品响应。
- `[1]` 与 `[ref_1]` 均可识别；已经是 Markdown link 的 `[1](...)`、双重方括号和非法/零索引不重复标注。

## 分层与数据流

```text
DashScope search_info
  -> ChatResult.search_sources
  -> build_url_citation_annotations(response_text, sources)
  -> AssistantTextOutput.annotations
  -> AgentResponse.annotations
  -> AgentRunResponse.annotations
  -> RealtimeAgentResult.annotations
  -> Gateway run.end.payload.annotations
```

Provider adapter 只归一化来源；Runtime citation 模块拥有角标解析与公共 annotation 模型；API 与 Gateway 只复制结构化字段，不根据正文二次推断。

## 兼容与安全

- `annotations` 默认空列表，因此旧调用方继续只读 `response_text`。
- 服务端不生成可执行 HTML，不跟随 URL，不抓取来源页面。
- URL 必须是绝对 HTTP(S) URL，标题和 URL 使用现有 Pydantic/JSON 转义输出。
- conversation history、TTS 与 token chunk 继续只保存/消费纯 `response_text`。

## 验证

- 临时 TDD 覆盖角标匹配、重复引用、未匹配/未引用来源、Markdown 防重复、Unicode offset。
- 临时 TDD 覆盖 `ChatResult -> AssistantTextOutput -> AgentResponse -> AgentRunResponse`。
- 临时 TDD 覆盖 `RealtimeAgentResult -> run.end.payload.annotations`。
- 只运行 mock/offline 测试，不调用真实 Provider。


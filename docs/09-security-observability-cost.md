# 09 安全、观测与成本控制

## 1. 安全

- 不把 API key 写进代码、测试或文档。
- 用户数据按 user_id 隔离。
- 视频、图片、记忆默认私有。
- 图片生成、视频理解、商品搜索结果都应预留内容审核接口。
- 真实支付、购买、下单类能力必须要求用户二次确认。

## 2. 观测

每次 Agent Run 记录：

- run_id
- user_id
- session_id
- intent
- selected_tools
- tool_calls
- latency_ms
- status
- error

每次 Tool Call 记录：

- call_id
- tool_name
- input 摘要
- output_ref
- status
- error
- latency_ms

## 3. 成本控制

- 视频理解限制抽帧数量。
- VLM、图片生成、渲染都是高成本工具，必须有配额和任务队列。
- 搜索/比价可以缓存。
- 长期记忆检索应限制 top_k。
- 日志记录 token、调用次数、外部 API 成本。

## 4. 降级策略

- VLM 不可用：询问用户上传关键图片或手动描述。
- 搜索不可用：基于视觉摘要生成搜索关键词。
- 图片生成失败：返回 prompt 供用户重试。
- 渲染排队：返回任务 ID 和预计状态，而不是阻塞。

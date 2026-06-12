# 08 测试与验收

## 1. 测试分层

### 单元测试

- Schema 校验
- IntentDetector
- ToolRouter
- ToolRegistry
- MemoryStore
- Mock Tools

### 集成测试

- `/agent/run` 完整调用
- 意图识别 → 工具选择 → 工具执行
- 记忆检索 → 当前任务规划

### 端到端测试

场景：

```text
用户上传视频并说：帮我找里面的鞋子，比较价格，再生成一张日系海报。
```

预期：

1. 调用视频理解
2. 调用商品搜索
3. 调用比价
4. 调用图片生成
5. 返回整合结果
6. 写入记忆

## 2. 验收标准

每个任务文件都必须定义 Acceptance。默认最低要求：

```bash
pytest
```

能够通过，或明确说明未通过原因。

## 3. 测试命名

```text
tests/unit/test_intent.py
tests/unit/test_router.py
tests/unit/test_tool_registry.py
tests/integration/test_agent_workflow.py
tests/e2e/test_demo_flow.py
```

## 4. Mock 数据

不要依赖真实外部 API。测试中使用稳定 mock：

- mock 视频理解返回“白色低帮运动鞋”。
- mock 商品搜索返回 3 个商品。
- mock 比价按价格排序。
- mock 图片生成返回本地 URL。
- mock 渲染返回 task_id 和 preview_url。

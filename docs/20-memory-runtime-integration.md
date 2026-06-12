# 20 Memory Runtime 集成设计

## 目标

把 JSONL MemoryStore 从单独能力接入主 Agent Runtime，使 Agent 支持跨会话、跨重启的本地记忆。

## 当前状态

已有：

```text
src/multimodal_agent/memory/store.py
src/multimodal_agent/memory/jsonl_store.py
```

但主 workflow 未统一使用长期 memory store。

## 目标流程

```text
UserRequest
  ↓
load_memory_node
  ↓
AgentState.memory_context
  ↓
plan / route / tool execution
  ↓
save_memory_node
  ↓
MemoryStore
```

## 配置建议

通过环境变量控制：

```text
MULTIMODAL_AGENT_MEMORY_BACKEND=memory|jsonl
MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/memories.jsonl
```

默认 backend 为 memory，测试中使用 tmp_path。

## 记忆写入策略

MVP 只保存结构化摘要，不保存大文件。

可保存：user_id、session_id、query、intent、selected_tools、final_response、key objects / products / generated assets、timestamp。

## 记忆检索策略

MVP 先做关键词和标签检索，不引入 embedding。后续 Phase 4 再考虑 Vector DB、Embedding、Hybrid Search。

## 验收标准

- Graph runtime 支持注入 memory store。
- JSONL store 可配置启用。
- 跨 runtime 实例可读取历史 memory。
- 单元测试不依赖固定本地路径。

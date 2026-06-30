# 本项目代码位置参考

---

## 核心目录结构

```
src/multimodal_agent/
├── services/context/          # 上下文工程核心
│   ├── builder.py            # 上下文组装、预算计算、裁剪执行
│   ├── policy.py             # 压缩触发策略
│   ├── compactor.py          # 摘要生成器
│   ├── compaction.py         # 工具观察裁剪
│   ├── renderer.py           # prompt 渲染
│   └── token_budget.py       # token 计数报告
│
├── schemas/context.py        # 上下文相关 schema 定义
├── schemas/tools.py          # 工具调用各层 schema
├── schemas/tool_observation.py # ToolObservation 实现
│
├── memory/
│   ├── manager.py            # 记忆加载、检索、写入
│   ├── write_policy.py       # 记忆写入策略、提升候选
│   └── store.py              # 持久化存储实现
│
└── agent/
    └── assistant_loop_nodes.py  # Agent 主循环节点
```

---

## 按功能模块查找

### 1. 上下文组装与预算控制

| 功能 | 文件 | 函数/类 |
|-----|------|--------|
| 上下文主入口 | `builder.py` | `build_assistant_context_pack()` |
| 预算计算 | `builder.py` | `_budget_report()` |
| 裁剪执行 | `builder.py` | `_enforce_context_budget()` |
| 工具观察裁剪降级 | `builder.py` | `_trim_observations_to_chars()` |

---

### 2. 压缩策略与触发

| 功能 | 文件 | 函数/类 |
|-----|------|--------|
| 压缩触发评估 | `policy.py` | `CompactionPolicy.evaluate()` |
| 6 种触发条件 | `policy.py` | 第 51-60 行 |
| 上下文摘要生成 | `compactor.py` | `DeterministicContextCompactor` |

---

### 3. 安全边界与裁剪

| 功能 | 文件 | 函数/类 |
|-----|------|--------|
| 工具观察裁剪 | `compaction.py` | `compact_observation_for_context()` |
| prompt 渲染边界标记 | `renderer.py` | 每个 section 开头标注"仅作为上下文数据" |
| 错误信息脱敏 | `provider_errors.py` | `sanitize_error_message()` |

---

### 4. 记忆写入策略

| 功能 | 文件 | 函数/类 |
|-----|------|--------|
| 提升候选评估 | `write_policy.py` | `MemoryWritePolicy.evaluate_promotion_candidate()` |
| 候选转正 | `write_policy.py` | `build_memory_item_from_promotion_candidate()` |
| 默认配置 | `write_policy.py` | `ContextPolicy` 类（第 82 行起） |

---

### 5. 可观测性字段

| 字段 | 含义 | 位置 |
|-----|------|------|
| `source_counts` | 各来源计数，排障第一站 | `builder.py` 第 211 行 |
| `budget.context_usage_ratio` | 上下文使用率 | `builder.py` 第 265 行 |
| `budget.compression_reasons` | 压缩触发原因 | `builder.py` 第 302-319 行 |
| `budget.trimmed_sections` | 被裁剪的 section | `builder.py` 第 435 行 |
| `context_summary_present` | 是否生成了会话摘要 | `builder.py` 第 102 行 |

---

## Debug 快速跳转

**怀疑上下文没有带上？**
→ 看 trace 里的 `source_counts`，如果都是 0 就是组装阶段出问题了

**怀疑压缩没有触发？**
→ 看 `budget.context_usage_ratio`，是不是真的超过 0.8 了
→ 看 `budget.compaction_triggered` 是不是 True

**怀疑 raw payload 泄漏进 prompt？**
→ 看 `ToolObservation.redacted` 是不是 True
→ 看 `compaction.pruned_keys` 都删了什么

**怀疑记忆被自动写入了？**
→ 看 `MemoryPromotionCandidate.rejected_reason`
→ 看 `allow_auto_write` 配置是不是被改成 True 了

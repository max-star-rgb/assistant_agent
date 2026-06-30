# 七、Agent Runtime 核心流程篇 - 详情

---

## 🔴 原题

你是新来的开发，接手这个项目。你发现一个诡异的 bug：

`AgentState` 里明明有完整的 10 轮对话历史（你在 debug 里看到了），但是用户说"我之前跟你说过我要白色的"时，LLM 却说"我没有看到你提过颜色偏好"。

查日志发现：LLM 收到的 prompt 里确实只有最近 2 轮对话，但是 `AgentState.conversation_history` 里明明有 10 轮。

请回答：
1. 这种"内存里有，prompt 里没有"的现象，根因可能是什么？请列出至少 3 种
2. 这个项目的上下文生命周期是怎样的？数据从 `ConversationStore` 到最终进入 prompt，中间经过了哪几层处理？
3. 怎么设计 Debug 工具，让开发一眼就能看到"每一层处理后还剩什么"？

---

## Q1. "内存里有，prompt 里没有"的上下文丢失问题 🔴

### 【我的回答】
1.根因可能是最近2轮之外的对话被裁剪和压缩，丢失了信息。2.上下文周期是只裁取最近2轮。数据经过了裁剪。3.不知道

### 【面试官点评】

✅ **答对的部分**：
- 第1题方向正确：确实是裁剪/压缩导致的
- 第2题摸到了"最近2轮"这个关键点

❌ **不足的地方**：
1. 第1题根因分析太浅，只说了"被裁剪了"，没说清楚**是在哪一层、被谁、以什么规则裁掉的**
2. 第2题完全没说对生命周期——"只裁取最近2轮"是结果，不是过程，中间至少还有4-5层处理
3. 第3题没思路，这是考察"可观测性设计"的核心题

---

### 【标准答案】

#### 1. 可能的 5 种根因（按排查顺序）

| 根因 | 具体表现 | 在哪一层 |
|-----|---------|---------|
| **1. ConversationStore 注入时只取了最近 N 轮** | 这个项目默认就是取最近 2 轮原文 + 较早的摘要，不是全量注入 | `assistant_run_service.py` 注入阶段 |
| **2. 上下文摘要压缩把关键信息弄丢了** | 较早的 8 轮对话被 `DeterministicContextCompactor` 压缩成了 1 句话摘要，刚好漏掉了"白色"这个关键词 | `compactor.py` 压缩阶段 |
| **3. 分栏渲染时被放到了"记忆块"里，LLM 没注意到** | 较早的对话被标成了"历史记忆"，但 LLM 注意力只看了"当前对话"那块 | `renderer.py` 渲染阶段 |
| **4. 内存泄漏/状态不一致** | 这一轮的 `AgentState` 和下一轮的不是同一个，或者多线程下状态被污染了 | `AgentState` 生命周期 |
| **5. 上游 API / websocket 丢包** | 前端发了，但后端没收到，或者只收到了部分字段 | 通信层 |

---

#### 2. 这个项目的完整上下文生命周期（**7 层处理**）

这是理解整个系统的关键！

```
1. ConversationStore 持久化层
   ↓（取历史）
2. 会话注入阶段（assistant_run_service）
   ↓（默认取最近 2 轮原文 + 较早轮压缩）
3. MemoryManager 记忆注入阶段
   ↓（检索 + 格式化）
4. 上下文组装阶段（builder.build_assistant_context_pack）
   ↓（各 section 拼接）
5. 预算计算阶段
   ↓（字符计数 + token 估计）
6. 裁剪执行阶段（_enforce_context_budget）
   ↓（按优先级裁剪）
7. Prompt 渲染阶段（renderer）
   ↓（最终进入 LLM）
```

**每一层可能丢信息的地方**：

| 层级 | 可能丢什么 | 关键代码 |
|-----|-----------|---------|
| 会话注入 | 较早轮对话直接被截断，只留摘要 | `request.metadata["conversation_context_recent_turns"] = 2` |
| 记忆注入 | 相似度不够的记忆不注入 | `MemoryManager.load_context_for_request()` |
| 组装阶段 | 字段漏传、key 名不对 | `build_assistant_context_pack()` |
| 预算计算 | token 计数不准，以为没超其实超了 | `token_budget.py` |
| 裁剪执行 | 超过预算直接删，不保留关键信息 | `_enforce_context_budget()` |
| 渲染阶段 | 分栏太靠后 LLM 注意力没覆盖到 | `render_prompt_json_context()` |

---

#### 3. Debug 工具设计：**上下文快照追踪器**

这是面试加分项，核心思想是：**每处理一层，就拍一张快照，最后做 diff**。

##### 设计方案：

```python
class ContextSnapshotTracer:
    """上下文全链路追踪器"""
    
    def __init__(self):
        self.snapshots = []  # 按顺序记录每一层后的状态
    
    def snapshot(self, layer_name: str, content: dict):
        """在某层处理后拍一张快照"""
        self.snapshots.append({
            "layer": layer_name,
            "content": content,
            "char_count": len(str(content)),
            "keywords_present": ["白色", "预算", "降噪"]  # 追踪关键词是否还在
        })
    
    def print_debug_report(self):
        """打印可视化报告"""
        print("=" * 80)
        print("上下文全链路追踪报告")
        print("=" * 80)
        
        for i, snap in enumerate(self.snapshots):
            print(f"\n[{i+1}] {snap['layer']}")
            print(f"    字符数: {snap['char_count']}")
            print(f"    关键存活: {snap['keywords_present']}")
            
            # 对比上一层，看看丢了什么
            if i > 0:
                prev = self.snapshots[i-1]
                lost = set(prev["keywords_present"]) - set(snap["keywords_present"])
                if lost:
                    print(f"    ⚠️  本层丢失关键词: {lost}")
    
    def export_to_trace(self, trace_id: str):
        """导出到 trace 系统，线上也能看"""
        pass
```

##### 关键插入点（拍快照的时机）：

```python
def build_assistant_context_pack(...):
    tracer = ContextSnapshotTracer()
    
    # 第1张快照：刚从请求里取出来的原始数据
    tracer.snapshot("原始请求注入", {"conversation": conversation_text, "memory": memory_text})
    
    # 组装各 section
    ...
    
    # 第2张快照：组装完成，还没算预算
    tracer.snapshot("组装完成", {...})
    
    # 执行裁剪
    budgeted = _enforce_context_budget(...)
    
    # 第3张快照：裁剪之后
    tracer.snapshot("裁剪完成", {...})
    
    # 渲染
    final_prompt = render_prompt_json_context(...)
    
    # 第4张快照：最终进入 LLM 的内容
    tracer.snapshot("最终 Prompt", final_prompt)
    
    # 如果是 debug 模式，直接打印报告
    if os.getenv("DEBUG_CONTEXT"):
        tracer.print_debug_report()
    
    # 把快照存入 trace，线上出问题了也能回溯
    return pack, tracer
```

##### 最终的 Debug 报告长这样：

```
================================================================================
上下文全链路追踪报告
================================================================================

[1] 原始请求注入
    字符数: 8000
    关键存活: {'白色', '预算', '降噪'}

[2] 组装完成
    字符数: 9500
    关键存活: {'白色', '预算', '降噪'}

[3] 裁剪完成
    字符数: 7800
    关键存活: {'预算', '降噪'}
    ⚠️  本层丢失关键词: {'白色'}

[4] 最终 Prompt
    字符数: 7850
    关键存活: {'预算', '降噪'}
```

**开发一眼就能看到**：哦，"白色"是在"裁剪完成"这一层丢的，直接去看裁剪逻辑就行，不用瞎猜了。

---

**面试金句**：
> 好的 Debug 工具不是让开发"努力找问题"，而是让问题"主动跳出来"。上下文丢信息不可怕，可怕的是你不知道在哪丢的。

---

### 【本项目代码位置】

- `src/multimodal_agent/services/assistant_run_service.py` - 会话注入阶段
- `src/multimodal_agent/services/context/builder.py` - 组装、预算、裁剪阶段
- `src/multimodal_agent/services/context/renderer.py` - Prompt 渲染阶段
- `src/multimodal_agent/services/context/compactor.py` - 摘要压缩阶段

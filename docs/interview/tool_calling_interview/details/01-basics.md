# 一、基础概念篇 - 详情

---

## 🔴 原题

**经典生产 bug 场景：**

用户说"帮我比较一下这几款耳机的价格"，系统里有两个工具：
- `product_search` - 搜索商品，返回商品列表
- `price_compare` - 接收商品列表，返回比价结果

但是 LLM 直接调用了 `price_compare`，传了个空列表进去，然后工具报错"没有商品可以比较"。

查日志发现：LLM 知道应该先搜再比价，但它"幻觉"自己已经调用过搜索了，直接跳了第二步。

请回答：
1. 这种"跳过前置步骤"的幻觉根因是什么？
2. 除了让 LLM"再想想"，有哪些工程手段可以从根本上避免？至少说 3 种
3. 这个项目里的 `ToolSpec` 设计有没有考虑这个问题？是怎么解决的？

---

## Q1. "跳过前置步骤"的工具调用幻觉问题 🔴

### 【我的回答】
1.之前调用过搜索;记忆错误注入。2.代码强制校验，必须存在搜索工具结果;强制限定llm只有搜索成功后，显示调用比价。3.本项目toolspec设计采用了问题2的思路

### 【面试官点评】

✅ **答对的部分**：
- 第2题方向正确：代码强制校验、状态机控制，这是最有效的工程手段
- 第3题方向正确：这个项目的 ToolSpec 确实有考虑这个问题

❌ **不足的地方**：
1. 第1题根因分析太浅：不是"之前调用过"或者"记忆错误"，本质是**LLM 没有"当前执行状态"的概念**——它不知道自己处在哪个步骤
2. 第2题只说了"强制校验"，没说清楚校验应该放在哪一层（是工具执行前拦截？还是 System Prompt 约束？）
3. 第3题没说具体是怎么解决的，这个项目用的是 `runtime_constraints` 字段

---

### 【标准答案】

#### 1. 根因分析：LLM 没有"状态机思维"

| 层面 | 具体问题 |
|-----|---------|
| **LLM 本质问题** | 每一轮推理都是无状态的，它不知道"之前做了什么，现在应该做什么，接下来还要做什么" |
| **工具描述问题** | ToolSpec 只说了"这个工具是干什么的"，没说"什么时候**不能**用这个工具" |
| **输出格式问题** | 只校验工具名和参数合法，不校验"当前上下文允不允许调用这个工具" |

**一句话根因**：
> LLM 是"无状态的推理机"，不是"有状态的执行器"。你不给它状态机约束，它就会跳步。

---

#### 2. 4 种工程解决手段（按效果排序）

| 手段 | 具体做法 | 效果 | 推荐度 |
|-----|---------|------|-------|
| **✅ 工具执行前状态机校验** | 不依赖 LLM 自觉，在 ToolExecutor 层硬卡：调用 `price_compare` 前，检查上下文中有没有 `product_search` 成功的观察结果，没有就直接拦截返回："你需要先调用 product_search 搜索商品" | 100% 防御 | ⭐⭐⭐⭐⭐ |
| **✅ ToolSpec 里写前置条件** | 在 `when_not_to_use` 字段里明确写："还没有调用 product_search 获得商品列表时，不要调用 price_compare"，LLM 会注意到 | 90% 有效 | ⭐⭐⭐⭐ |
| **✅ System Prompt 加流程约束** | 明确写步骤顺序："比价必须分两步：第一步搜索获得商品列表，第二步才比价，绝对不能一步到位" | 80% 有效 | ⭐⭐⭐ |
| **✅ 少样本示例引导** | 在 System Prompt 里放一个正确的调用顺序示例 | 70% 有效 | ⭐⭐ |

**最有效组合：状态机校验（兜底） + when_not_to_use（预防）**

---

#### 3. 这个项目的 ToolSpec 设计：`runtime_constraints` 字段

这个项目确实考虑了这个问题，做法是给每个 ToolSpec 加了 3 个特殊字段：

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict
    
    # 👇 这三个字段就是为了解决跳步问题 👇
    when_to_use: list[str]    # 什么时候应该用
    when_not_to_use: list[str]  # 什么时候绝对不能用
    runtime_constraints: list[str]  # 执行前必须满足的约束
```

**以 `price_compare` 为例的实际配置**：

```python
{
    "name": "price_compare",
    "when_to_use": [
        "用户明确要求比价",
        "product_search 已经成功返回了商品列表"
    ],
    "when_not_to_use": [
        "还没有调用 product_search 获得商品列表",
        "用户只是问有没有货，不是要比价"
    ],
    "runtime_constraints": [
        "调用前必须检查 observations 里有没有 product_search 的成功结果",
        "如果没有，返回错误并提示应该先调用 product_search"
    ]
}
```

**实际执行时的校验逻辑**：

```python
def validate_tool_call(tool_name: str, observations: list) -> tuple[bool, str]:
    """工具执行前的前置校验"""
    spec = get_tool_spec(tool_name)
    
    for constraint in spec.runtime_constraints:
        if "product_search 的成功结果" in constraint:
            has_product_search = any(
                obs["tool_name"] == "product_search" 
                and obs["status"] == "succeeded"
                for obs in observations
            )
            if not has_product_search:
                return False, "必须先调用 product_search 获得商品列表才能比价"
    
    return True, "OK"
```

**设计亮点**：
- 约束不是写死在代码里的，而是写在 `ToolSpec` 数据里
- 新增工具时，开发者自己写清楚前置条件
- LLM 能看到这些约束（渲染在 prompt 里），ToolExecutor 也会校验这些约束
- **人和机器读同一份约束文档，避免两边理解不一致**

---

**面试金句**：
> 不要相信 LLM 的"自觉"，也不要指望"把 System Prompt 写得更详细就能解决"。真正可靠的系统是：**LLM 哪怕想做错，系统也不让它做错**。

---

### 【本项目代码位置】

- `src/assistant_agent/schemas/tools.py` - ToolSpec 定义
- `src/assistant_agent/agent/tool_executor.py` - 工具执行校验逻辑

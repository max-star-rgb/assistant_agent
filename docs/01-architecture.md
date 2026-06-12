# 01 系统总体架构

## 1. 定位

本项目不是单一视频理解系统，也不是单一商品搜索系统，而是一个多模态自主工具调用 Agent。视频理解、图片生成、3D 渲染、商品搜索、比价、记忆检索都是 Agent 可以调用的能力。

## 2. 核心链路

```text
User Input
  ├─ text
  ├─ image
  ├─ video
  └─ audio/asr
      ↓
Input Normalizer
      ↓
Perception Layer
  ├─ LLM text reasoning
  ├─ VLM image understanding
  ├─ Video MLLM / frame understanding
  └─ ASR/OCR adapters
      ↓
Agent Core
  ├─ Memory Retrieval
  ├─ Intent Detection
  ├─ Task Planner
  ├─ Tool Router
  ├─ Tool Executor
  └─ Response Composer
      ↓
Capability Services
  ├─ product search
  ├─ price compare
  ├─ image generation
  ├─ 3D rendering
  ├─ memory save/search
  └─ notification/database tools
```

## 3. 设计原则

1. Agent 只做编排，不把具体能力写死在 planner 中。
2. 所有工具通过统一 Tool Contract 接入。
3. 所有模型能力先抽象成 adapter，开发阶段使用 mock。
4. AgentState 是唯一的流程状态载体。
5. 长耗时任务必须可跟踪：状态、进度、错误、输出地址。
6. 记忆必须可解释：为什么命中、命中了什么、如何参与当前决策。

## 4. 关键模块

### 4.1 Input Normalizer

把用户输入规范化为结构化请求：

```python
UserRequest(
    user_id="u1",
    session_id="s1",
    text="帮我找这个鞋子的相似款并比价",
    image_ids=[],
    video_ids=["v1"],
    audio_id=None,
)
```

### 4.2 Perception Layer

负责把图片/视频/语音转成 Agent 可用的语义摘要。

输出不是自由文本，而是结构化结果：

```python
VisualUnderstandingResult(
    objects=["白色低帮运动鞋"],
    colors=["白色"],
    materials=["皮革", "橡胶"],
    scene="室内桌面展示",
    style_tags=["简约", "日系"],
    text_in_media=[],
    summary="视频中展示了一双白色低帮运动鞋。",
)
```

### 4.3 Agent Core

核心决策顺序：

```text
Load State
  ↓
Retrieve Memory
  ↓
Understand Current Input
  ↓
Detect Intent
  ↓
Plan Task
  ↓
Select Tool(s)
  ↓
Execute Tool(s)
  ↓
Merge Results
  ↓
Save Memory
  ↓
Respond
```

### 4.4 Capability Services

外部能力全部服务化或 adapter 化。开发初期可以是本地 mock，生产环境再替换为真实 API。

## 5. MVP 边界

MVP 不要求真实接入全部模型。优先实现：

1. 统一 AgentState
2. 意图识别规则 + mock LLM 分类
3. Tool Registry
4. Mock VLM、Mock 搜索、Mock 比价、Mock 图片生成、Mock 渲染
5. FastAPI 演示接口
6. 一个端到端流程

MVP 通过后，再逐步替换具体工具实现。

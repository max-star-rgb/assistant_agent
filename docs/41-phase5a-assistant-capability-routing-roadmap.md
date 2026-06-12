# 41 Phase 5A 路线图：Assistant Capability Routing Baseline

## 背景

当前项目不是 Video-first Agent，也不是 Vision-first Agent，而是 Intent-driven Assistant Agent。

Agent 的核心职责是：

```text
理解用户意图
  ↓
判断是否需要调用能力
  ↓
选择 direct_chat / image_generation / image_understanding / video_understanding / product_search / price_compare / render_3d / memory_retrieval / multi_step_orchestration
  ↓
执行能力或多步任务
  ↓
整合结果返回用户
```

真实 Qwen Vision smoke 已经跑通，这是一个重要能力验证，但它只是 image_understanding / video_understanding Provider 的验证结果，不是 Phase 5A 主线。

## Phase 5A 总目标

Phase 5A 的目标是建立“助理 Agent 能力路由基线”，保证系统能根据用户意图正确选择能力，并且不要求所有任务都依赖图片或视频输入。

核心要求：

1. `direct_chat` 支持纯文本输入。
2. `image_generation` 支持纯文本输入。
3. `product_search` 和 `price_compare` 支持纯文本输入，也可结合图片/视频理解结果。
4. `image_understanding` 只在用户意图需要看图、解释图像、识别图片内容时触发。
5. `video_understanding` 只在用户意图需要理解视频时触发。
6. `render_3d` 支持文本描述、商品信息或图像上下文作为输入。
7. `memory_retrieval` 支持“上次那个”“之前的商品”“我喜欢的风格”等历史指代。
8. 多步任务由 planner / graph loop 执行，而不是单步工具误判。

## 能力矩阵

| Capability | 是否需要文本 | 是否需要图片 | 是否需要视频 | 说明 |
|---|---:|---:|---:|---|
| direct_chat | 是 | 否 | 否 | 普通聊天、文案、解释、建议 |
| image_generation | 是 | 否 | 否 | 文生图；也可结合图片上下文做图生图或参考图生成 |
| image_understanding | 可选 | 是 | 否 | 看图、识别物体、解释图片 |
| video_understanding | 可选 | 否 | 是 | 看视频、总结视频、识别视频事件 |
| product_search | 是 | 否 | 否 | 文本搜商品；也可结合视觉结果搜同款/相似款 |
| price_compare | 是 | 否 | 否 | 根据商品候选或文本条件比价 |
| render_3d | 是 | 可选 | 可选 | 渲染场景、3D 预览、模型展示 |
| memory_retrieval | 是 | 否 | 否 | 检索历史偏好、历史商品、历史任务 |
| multi_step_orchestration | 是 | 可选 | 可选 | 多工具组合任务 |

## 路由原则

### 文本优先

用户的明确文本意图优先于媒体输入。

例如用户上传图片并说：

```text
用这张图生成一张电商海报
```

不应只路由到：

```text
image_understanding
```

而应路由到：

```text
image_understanding → image_generation
```

### 纯文本能力不得要求媒体

以下请求不需要图片或视频：

```text
帮我生成一张赛博朋克风格海报
帮我写一段商品宣传文案
帮我找 500 元以内的白色运动鞋
帮我比较一下 iPhone 15 和 iPhone 16 的价格
```

### 媒体不自动等于理解任务

只要有图片/视频输入，不代表一定要执行 image_understanding/video_understanding。必须结合用户意图判断。

### 歧义时追问

如果用户只上传图片但没有说明目的，可以追问：

```text
我看到你上传了一张图片。你是想让我解释图片内容、找相似商品，还是基于它生成图片？
```

## Phase 5A 任务顺序

```text
041 Assistant Capability Routing Roadmap
042 Intent Taxonomy and Capability Contracts
043 Text-only Routing Baseline
044 Media-aware Routing Baseline
045 Multi-step Orchestration Baseline
046 Routing Evals and Regression Suite
047 Phase 5A Assistant Routing Review
```

## 旧 Vision-only 文档处理

旧的 Vision-only 041-046 不应继续作为 Phase 5A 主线。

处理原则：

- 已完成的真实 Qwen Vision smoke 结果应保留为 Provider validation 记录。
- Vision response mapping 和 Provider safety 文档可作为附属资料保留。
- 后续真实 Vision hardening 可归入 Provider Hardening 阶段，而不是 Assistant Routing Baseline。

## 当前任务边界

Phase 5A 的当前主线任务只验证 Assistant capability routing：

- 纯文本 direct_chat 不应要求图片或视频。
- 纯文本 image_generation 不应要求图片或视频。
- 商品搜索、比价、记忆检索和 3D 渲染应能按文本意图触发。
- 图片/视频输入只是上下文信号，不能覆盖用户明确文本意图。
- 多动作请求应进入 multi_step_orchestration。

真实 Provider 的进一步稳定化、批量 smoke、trace/debug 和成本控制应另设 Provider Hardening 阶段，不应阻塞 Assistant Routing Baseline。

# 84 Phase 5G 路线图：Video Understanding as External MLLM Capability

## 背景

当前项目已经完成：

```text
Phase 5A Assistant Capability Routing Baseline
Phase 5B Text-first Capabilities
Phase 5C Product Search / Price Compare
Phase 5D Render / 3D Capability
Phase 5E End-to-End Demo Flow
Phase 5F Hybrid Intent Router & Planner Quality
```

系统已经具备一个 Intent-driven Assistant Agent 的主要能力框架：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
multi_step_orchestration
```

Phase 5G 聚焦补齐 `video_understanding` 的能力边界，但必须保持轻量。视频理解不是本项目的模型研究主线，而是 Agent 调用的外部多模态模型能力。

## Phase 5G 总目标

让 Agent 能根据用户意图调用外部 Video MLLM / VLM Provider 完成视频理解，并把结构化结果传递给后续能力。

核心链路：

```text
UserRequest(text + video_ref)
  ↓
Intent Router
  ↓
CapabilityValidator
  ↓
video_understanding
  ↓
VideoUnderstandingTool
  ↓
VideoUnderstandingAdapter
  ↓
External Video MLLM Provider
  ↓
VideoUnderstandingResult
  ↓
Agent Planner / Response Composer / Downstream Tools
```

## Agent 负责什么

Agent 负责：

1. 判断用户是否需要视频理解。
2. 校验是否存在 video / video_ref。
3. 判断是单步视频理解还是多步任务。
4. 构造 VideoUnderstandingRequest。
5. 调用 VideoUnderstandingTool。
6. 将 VideoUnderstandingResult 交给后续能力使用。

## 外部 Video MLLM 负责什么

外部 Provider 负责：

```text
视频总结
物体识别
商品识别
场景识别
动作/事件识别
视频中文字识别
品牌/颜色/材质提取
与用户问题相关的视频问答
```

Agent 不研究：

```text
视频编码
时序建模
多模态对齐
视频 Transformer
视频大模型训练
```

## Phase 5G 明确不做

本阶段不做：

- 自研视频理解模型。
- 训练 Video MLLM。
- 复杂抽帧系统。
- 实时 WebRTC。
- 视频监控平台。
- 视频数据库。
- 长视频切片平台。
- 多路视频流处理。
- 大规模视频存储。
- 自动上传隐私视频到真实 Provider。
- 默认调用真实 Video Provider。

## Phase 5G 要做什么

本阶段只做轻量能力接入：

1. 定义 `VideoUnderstandingRequest` / `VideoUnderstandingResult`。
2. 定义 `VideoUnderstandingAdapter` contract。
3. 保持默认 `MockVideoUnderstandingAdapter`。
4. 可预留 `HttpVideoUnderstandingAdapter` skeleton。
5. 明确直接视频 Provider 与抽帧 fallback 的边界。
6. 支持视频理解结果传给 product_search / price_compare / image_generation / render_3d / memory_save。
7. 增加默认 mock smoke / eval / API 覆盖。
8. 生成 Phase 5G 审计报告。

## 两种 Provider 模式

### Provider 直接支持视频

```text
video_ref / video_url / local video path
  ↓
External Video MLLM
  ↓
VideoUnderstandingResult
```

### Provider 只支持图片

Adapter 内部可做轻量 fallback：

```text
video
  ↓
sample frames inside adapter
  ↓
Image VLM
  ↓
merge frame results
  ↓
VideoUnderstandingResult
```

注意：抽帧 fallback 是 Adapter 内部细节，Agent 层不感知。

## Phase 5G 任务顺序

```text
081 Phase 5G Video Understanding Roadmap
082 Video Understanding Request / Result / Adapter Baseline
083 Video Provider Adapter Skeleton and Safety
084 Video Multistep Integration
085 Video Smoke / Eval / API Coverage
086 Phase 5G Review
```

## 默认安全边界

- 默认使用 MockVideoUnderstandingAdapter。
- 默认 pytest 不调用真实 Video Provider。
- 默认 eval 不调用真实 Video Provider。
- Demo runner 默认不调用真实 Video Provider。
- 真实 Video Provider 只能用户显式配置环境变量并手动运行 smoke 或 env-gated integration tests。
- 不写入 API Key。
- 不提交真实视频。
- 不提交视频理解真实 Provider raw response。
- 不输出完整 base64、Authorization header 或 Bearer token。

## Phase 5G 完成后

Phase 5G 完成后，后续再考虑：

```text
Phase 5H Provider Safety / Retry / Cost / Trace Query
Phase 5I Memory Hardening
Phase 5J MCP / Skills Packaging
```

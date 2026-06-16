# 48 Phase 5B 路线图：Text-first Capabilities

## 背景

Phase 5A 已完成 Assistant Capability Routing Baseline。系统已经明确为 Intent-driven Assistant Agent，并能根据用户意图选择：

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

Phase 5B 不继续做 Vision-only，也不一次性接入所有真实 Provider。Phase 5B 聚焦最核心的纯文本能力：

```text
direct_chat
image_generation
```

原因：

- 用户可以只输入文本，不上传图片或视频。
- direct_chat 是助理 Agent 的基础能力。
- image_generation 是用户明确提到的关键能力之一。
- 这两个能力能快速把项目从“路由正确”推进到“用户可玩”。

## Phase 5B 总目标

让助理 Agent 在纯文本输入下具备更真实、稳定、可测试的：

1. 直接聊天能力。
2. 文生图能力。
3. Prompt 构造能力。
4. 文本能力输出协议。
5. 手动 smoke 测试入口。
6. 默认 Mock、默认离线、可选真实 Provider 的安全边界。

Phase 5B 的验收对象只包括：

```text
direct_chat
image_generation
```

其他已经存在的能力仍保留为 Phase 5A routing baseline 的一部分，但不在 Phase 5B 中继续扩展真实能力、Provider 接入或 smoke 流程。

## 重点边界

Phase 5B 不要求默认调用真实 Provider。

允许：

```text
新增 LLM Chat Adapter skeleton
新增 Image Generation Adapter skeleton
新增 mock/local deterministic 实现
新增 provider config
新增 smoke 脚本
新增 env-gated integration tests
```

禁止：

```text
默认调用真实 API
写入 API Key
提交真实生成图片
自动安装依赖
默认 pytest 调用真实 Provider
把图片生成与视频理解强绑定
接入商品搜索真实 Provider
接入比价真实 Provider
接入 3D 渲染真实 Provider
新增 Vision hardening 主线
```

## Text-first 能力边界

### direct_chat

`direct_chat` 必须支持纯文本输入，例如：

```text
帮我写一段商品介绍
解释一下 Agent 和 Tool 的区别
给我三个营销文案方向
```

默认不应调用图片理解、视频理解、商品搜索、比价或 3D 渲染。

### image_generation

`image_generation` 必须支持纯文本输入，例如：

```text
生成一张赛博朋克风格海报
帮我做一张日系极简商品图
做一张适合小红书的封面
```

默认不应要求用户上传图片或视频。图片/视频只能作为可选上下文，不能成为纯文本文生图的前置条件。

## Phase 5B 任务顺序

```text
048 Phase 5B Text-first Capabilities Roadmap
049 Direct Chat Provider Adapter
050 Image Generation Provider Adapter
051 Prompt and Output Contracts
052 Text-only Image Generation Smoke
053 Text Capability Evals and API Coverage
054 Phase 5B Review
```

## 与后续阶段关系

Phase 5B 完成后再决定：

```text
5C 商品搜索 / 比价真实能力
5D 3D 渲染真实能力
5E Provider Hardening / Retry / Cost
5F Memory Hardening
5G Harness Engineering
```

不要现在一次性执行后续阶段。

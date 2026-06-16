# 62 Phase 5D 路线图：Render / 3D 渲染能力基线

## 背景

Phase 5A 已完成 Assistant Capability Routing Baseline。

Phase 5B 已完成：

```text
direct_chat
image_generation
```

Phase 5C 已完成：

```text
product_search
price_compare
```

接下来进入 Phase 5D：

```text
render_3d
```

但 Phase 5D 必须轻量化。当前项目是 Intent-driven Assistant Agent，不是独立 3D 渲染平台。因此 Phase 5D 的目标不是实现 Blender / Unity / Three.js 生产级渲染系统，而是让 Agent 能把 `render_3d` 当作一个稳定 capability 调度。

## Phase 5D 总目标

让助理 Agent 支持以下场景：

```text
把一把浅灰色沙发放到北欧风客厅里看看
```

```text
用刚才搜到的椅子生成一个 3D 展示
```

```text
看这张图里的商品，然后放到卧室场景里渲染一下
```

```text
总结这个视频里的商品，并生成一个展厅 3D 预览
```

对应能力链路：

```text
text → render_3d
product_search → render_3d
image_understanding → render_3d
video_understanding → render_3d
```

## Phase 5D 明确不做

本阶段不做：

- 真实 Blender 渲染。
- 真实 Unity 渲染。
- Three.js 前端编辑器。
- 复杂材质系统。
- 3D 模型资产管理平台。
- 生产级任务队列。
- 复杂相机轨道系统。
- 渲染农场。
- 真实付费渲染服务。
- 大规模 3D 数据存储。
- 自动下载模型或资源。

## Phase 5D 要做什么

本阶段只做轻量能力接入：

1. 定义 `RenderRequest` / `RenderResult`。
2. 定义 `RenderAdapter` contract。
3. 保持默认 `MockRenderAdapter`。
4. 可预留 `HttpRenderAdapter` skeleton。
5. 支持纯文本场景描述进入 `render_3d`。
6. 支持商品结果、图片理解结果、视频理解结果作为渲染上下文。
7. 增加 API / Eval / Smoke 覆盖。
8. 生成 Phase 5D 审计报告。

## 推荐执行顺序

```text
062 Phase 5D Render Capability Roadmap
063 Render Request / Result / Adapter Baseline
064 Render Input Contract and Multistep Integration
065 Render Smoke / Eval / API Coverage
066 Phase 5D Review
```

## 默认安全边界

- 默认使用 MockRenderAdapter。
- 默认 pytest 不调用真实渲染服务。
- 默认 eval 不调用真实渲染服务。
- 真实 render provider 只能通过用户显式配置环境变量并运行 smoke 脚本触发。
- 不写入 API Key。
- 不提交真实模型文件、大型渲染结果或生成视频。
- 渲染输出目录必须被 `.gitignore` 忽略。

## 与后续阶段关系

Phase 5D 完成后，核心能力基本齐全：

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

下一阶段建议不要继续无限扩 Provider，而是进入：

```text
Phase 5E End-to-End Demo Flow
```

目标是把多个能力串成可演示的完整用户场景。

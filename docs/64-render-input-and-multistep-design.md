# 64 Render Input and Multistep Design

## 目标

明确 `render_3d` 如何接收来自不同 capability 的上下文，并在多步任务中稳定运行。

## 支持的输入来源

### 1. 纯文本

用户：

```text
把一把浅灰色布艺沙发放到北欧风客厅里看看
```

链路：

```text
text → render_3d
```

### 2. 商品搜索结果

用户：

```text
帮我找一款黑色办公椅，然后放到现代办公室里看看
```

链路：

```text
product_search → render_3d
```

`render_3d` 应使用：

```text
ProductResult.title
ProductResult.image_url
ProductResult.product_url
ProductResult.category
ProductResult.reason
```

### 3. 图片理解结果

用户上传图片并说：

```text
把图里的这个商品放到卧室里渲染一下
```

链路：

```text
image_understanding → render_3d
```

`render_3d` 应使用：

```text
visual_summary
objects
colors
materials
style_tags
image_ref / output_ref
```

### 4. 视频理解结果

用户上传视频并说：

```text
把视频里的商品做一个展厅 3D 展示
```

链路：

```text
video_understanding → render_3d
```

`render_3d` 应使用：

```text
video_summary
objects
actions
scene
video_ref / output_ref
```

### 5. 记忆检索结果

用户：

```text
把上次那个黑色包放到极简客厅里看看
```

链路：

```text
memory_retrieval → render_3d
```

## Tool Input Builder 要求

`build_tool_input()` 或等价逻辑应支持把上游结果转为 RenderRequest：

```text
product_search result → product_ref/product_title/product_image_url
image_understanding result → visual_summary/image_ref
video_understanding result → video_summary/video_ref
memory result → product_ref/style/context
```

## Planner 要求

多步 planner 应能识别：

```text
搜索 + 渲染
看图 + 渲染
看视频 + 渲染
记忆检索 + 渲染
```

## Graph Loop 要求

LangGraph loop 不应把 render 作为特殊流程硬编码成平台逻辑，只需要把它作为普通 plan step：

```text
PlanStep(capability="render_3d")
```

## 验收标准

- text → render_3d 可运行。
- product_search → render_3d 可运行。
- image_understanding → render_3d 可运行。
- video_understanding → render_3d 可运行。
- memory_retrieval → render_3d 可运行。
- 默认 mock，不调用真实服务。

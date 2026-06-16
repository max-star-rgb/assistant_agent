# 87 Video Multistep Integration

## 目标

让 `video_understanding` 的结果可以稳定传给后续 capabilities，形成多步任务链路。

## 支持链路

### 1. 单步视频理解

用户：

```text
总结这个视频。
```

链路：

```text
video_understanding
```

### 2. 视频找商品

用户：

```text
找一下视频里的商品。
```

链路：

```text
video_understanding → product_search
```

### 3. 视频找商品并比价

用户：

```text
找视频里的商品，并比较价格。
```

链路：

```text
video_understanding → product_search → price_compare
```

### 4. 视频生成海报

用户：

```text
根据这个视频里的商品生成一张宣传海报。
```

链路：

```text
video_understanding → image_generation
```

### 5. 视频进入 3D 渲染

用户：

```text
把视频里的商品做一个展厅 3D 展示。
```

链路：

```text
video_understanding → render_3d
```

### 6. 视频记忆保存

用户：

```text
记住这个视频里的商品风格。
```

链路：

```text
video_understanding → memory_save
```

## Tool Input Builder 要求

`build_tool_input()` 或等价逻辑应支持：

```text
video_understanding result → product_search.visual_summary/video_summary
video_understanding result → image_generation.video_summary/product_context
video_understanding result → render_3d.video_summary/video_ref
video_understanding result → memory_save.summary/style/products
```

## Planner 要求

Planner 应识别：

```text
视频总结
视频找商品
视频比价
视频生成图片
视频渲染
视频记忆
```

并生成有序 plan steps。

## CapabilityValidator 要求

如果用户要求视频理解但没有 video_ref：

```text
ask_followup
```

如果用户提供 video_ref 但意图是普通聊天：

```text
direct_chat
```

即视频输入不应压倒文本意图。

## Response Composer 要求

响应应具体说明视频理解结果。

不要只说：

```text
已完成请求处理。
```

应根据结果输出：

```text
我总结了视频内容，识别到主要商品为白色低帮运动鞋，并提取到室内商品展示场景。接下来已基于这些信息搜索了相似商品。
```

## 验收标准

- video_understanding 单步可运行。
- video_understanding → product_search 可运行。
- video_understanding → price_compare 可运行。
- video_understanding → image_generation 可运行。
- video_understanding → render_3d 可运行。
- 缺 video 时 ask_followup。
- 有 video 但文本为普通聊天时不强制理解视频。
- 默认 mock。

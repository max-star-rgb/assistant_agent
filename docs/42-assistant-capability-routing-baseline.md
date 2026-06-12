# 42 Assistant Capability Routing Baseline

## 目标

建立稳定的 capability routing 规则，让 Agent 能根据用户意图选择正确能力，而不是被输入模态牵引。

## 输入结构

UserRequest 可能包含：

```text
text
images
videos
audio
session_id
user_id
metadata
```

其中：

- text 是意图判断的主信号。
- images/videos 是上下文信号。
- metadata 可包含历史任务、来源、偏好、语言、provider config 等。

## Capability 定义

Phase 5A 使用以下 canonical intent / capability 名称：

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
ask_followup
```

历史命名仍作为 alias 兼容：

```text
chat -> direct_chat
generate_image -> image_generation
understand_image -> image_understanding
understand_video -> video_understanding
retrieve_memory -> memory_retrieval
multi_tool_task -> multi_step_orchestration
```

业务代码应优先使用 canonical 名称；兼容层用于保护既有测试、历史 API payload 和 eval case。

### direct_chat

用于：

- 普通聊天
- 文案生成
- 概念解释
- 建议
- 总结用户文本
- 不需要外部工具的问答

输入要求：

```text
text required
media optional but not required
```

### image_generation

用于：

- 文生图
- 海报生成
- 风格图生成
- 商品图生成
- 图片改造任务的最终生成步骤

输入要求：

```text
text required
image optional
```

### image_understanding

用于：

- 图片内容识别
- 图像问答
- 从图片提取商品/场景/风格信息
- 为后续搜索、比价、生成、渲染提供视觉上下文

输入要求：

```text
image required
text optional
```

### video_understanding

用于：

- 视频总结
- 视频问答
- 视频事件检测
- 从视频提取商品、动作、场景、文字、语音信息

输入要求：

```text
video required
text optional
```

### product_search

用于：

- 文本商品搜索
- 同款/相似款搜索
- 根据视觉理解结果搜索商品
- 根据预算、品牌、风格、平台条件搜索

输入要求：

```text
text required OR visual_summary required
```

### price_compare

用于：

- 商品候选比价
- 多平台价格比较
- 预算筛选
- 推荐最优购买方案

输入要求：

```text
product candidates OR search query required
```

### render_3d

用于：

- 3D 场景渲染
- 商品放入房间/场景预览
- 模型渲染
- 多角度展示

输入要求：

```text
scene description required
product/model/image optional
```

### memory_retrieval

用于：

- 历史指代
- 用户偏好
- 上次任务
- 已保存商品/图片/渲染结果

输入要求：

```text
text required
session/user context required
```

### multi_step_orchestration

用于：

- 一句话包含多个动作
- 一个动作依赖另一个动作结果
- 需要多个工具顺序执行

示例：

```text
帮我找这张图里的鞋子，比较价格，再生成一张海报
```

对应：

```text
image_understanding → product_search → price_compare → image_generation
```

## 路由优先级

推荐优先级：

1. 显式多步请求优先。
2. 显式生成请求优先于单纯理解媒体。
3. 显式搜索/比价请求优先于单纯理解媒体。
4. 有媒体但文本意图不明确时，追问。
5. 无媒体但请求生成图片时，走 text-only image_generation。
6. 无媒体但请求聊天/解释/文案时，走 direct_chat。
7. 包含“上次/之前/我喜欢的”等历史指代时，加入 memory_retrieval。

## Fallback 策略

- 意图不明确：ask_followup。
- 缺少必要媒体：请求用户上传图片/视频。
- 缺少搜索条件：追问预算/品牌/平台/用途。
- 工具失败：根据 RecoveryPolicy 部分完成或返回结构化错误。

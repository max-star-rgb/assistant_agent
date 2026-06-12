# 43 Direct Chat 与纯文本能力设计

## 目标

确保助理 Agent 不依赖图片或视频也能工作。

Phase 5A 必须确认以下能力支持纯文本输入：

```text
direct_chat
image_generation
product_search
price_compare
memory_retrieval
render_3d
```

其中 direct_chat 和 image_generation 是最关键的文本能力。

## Direct Chat

### 场景

```text
帮我写一段商品介绍
这个风格适合年轻用户吗？
给我一个项目介绍
帮我解释一下 Agent 和 Tool 的区别
```

### 不应调用

默认不应调用：

```text
image_understanding
video_understanding
product_search
price_compare
render_3d
```

除非用户明确要求。

### 输出

可以直接由 LLM / response composer 生成。

若当前项目没有真实 LLM Provider，可以先用 deterministic / mock response，但 route intent 必须正确。

## Text-only Image Generation

### 场景

```text
生成一张赛博朋克风格海报
帮我生成一张日系极简商品图
做一张适合小红书的封面
```

### 要求

不应要求用户上传图片。

应进入：

```text
image_generation
```

而不是：

```text
image_understanding
```

### 输入

```text
prompt
style
size optional
negative_prompt optional
```

## Text-only Product Search

### 场景

```text
帮我找 500 元以内的白色运动鞋
找一款适合通勤的黑色双肩包
搜索一下人体工学椅
```

应进入：

```text
product_search
```

不需要 image_understanding。

## Text-only Price Compare

### 场景

```text
比较一下 iPhone 15 和 iPhone 16 的价格
帮我找同款里最便宜的
这几款耳机哪个性价比高
```

若没有候选商品，可以先搜索，再比价：

```text
product_search → price_compare
```

## Text-only Render

### 场景

```text
把一把浅灰色布艺沙发放到北欧风客厅里看看
生成一个现代风展厅里的产品 3D 预览
```

如果没有 3D 模型，可走 render_3d 的 mock / provider skeleton，并明确缺少模型或使用占位模型。

## 验收标准

- 纯文本聊天不会要求图片。
- 纯文本生图不会要求图片。
- 纯文本搜商品不会要求图片。
- 有媒体输入时，文本意图仍优先。

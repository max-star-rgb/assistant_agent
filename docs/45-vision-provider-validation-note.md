# 45 Vision Provider Validation Note

## 目的

记录真实 Qwen Vision smoke 已跑通，但明确它不是 Phase 5A 主线。

## 已验证内容

用户手动运行真实 Qwen Vision smoke，观察到：

```text
provider = qwen
output_ref = provider://vision/qwen
vision_result 返回 objects/colors/materials/scene/style_tags/text_in_media/summary
exit code = 0
```

这说明：

```text
Qwen Vision Provider
  ↓
HttpVisionProviderAdapter
  ↓
VisionUnderstandingTool
  ↓
AgentGraphRuntime
```

基础链路可用。

## 新定位

该验证结果属于：

```text
Provider Validation / Capability Validation
```

不是：

```text
Assistant Product Mainline
```

Phase 5A 主线仍是：

```text
Assistant Capability Routing Baseline
```

## 与 Phase 5A 的关系

Phase 5A 不以 Vision Provider 为中心。真实 Qwen Vision smoke 的意义是证明 `image_understanding` capability 可以接入真实 Provider；它不改变 Assistant 的产品主线。

因此 Phase 5A 的验收不应要求继续增加真实 Vision 功能，而应验证：

- 用户纯文本聊天能走 direct_chat。
- 用户纯文本生图能走 image_generation。
- 用户纯文本搜商品、比价、渲染、检索记忆能走对应 capability。
- 用户明确要求看图/看视频时才触发 image_understanding/video_understanding。
- 媒体输入和文本意图冲突时，文本意图优先。
- 多步请求由 planner/graph loop 编排。

## 后续用途

真实 Vision 验证结果可用于：

- image_understanding capability 的 provider confidence。
- 后续 video_understanding provider 扩展参考。
- 后续 Provider hardening 阶段的基础样例。
- 后续真实数据 smoke regression。

这些后续工作不属于当前 Assistant Routing Baseline 的必做主线。

## 注意事项

- 不提交真实图片。
- 不提交 API Key。
- 默认测试仍用 Mock。
- 真实 Provider 只能由用户显式运行 smoke 脚本触发。

# 47 Phase 5A Assistant Routing Review

## 1. Assistant Agent 定位

Phase 5A 已将项目主线从 Vision-first / Video-first 调整为 Intent-driven Assistant Agent。

Agent 的职责是根据用户意图选择和编排能力，而不是被输入模态牵引。图片、视频和历史记忆都是上下文信号；最终是否调用工具由文本意图、必要输入和多步依赖共同决定。

当前默认运行路径仍是离线 Mock/Local 能力，不默认调用真实 Provider。

## 2. Capability Matrix

| Capability | 纯文本 | 图片上下文 | 视频上下文 | 当前状态 |
|---|---:|---:|---:|---|
| `direct_chat` | 支持 | 可忽略媒体直接聊天 | 可忽略媒体直接聊天 | 已接入 routing |
| `image_generation` | 支持 | 可结合图片理解结果生成 | 可作为多步后续生成 | 已接入 mock adapter |
| `image_understanding` | 需要图片 | 支持 | 不适用 | 已接入 mock/可选真实 Provider |
| `video_understanding` | 需要视频 | 不适用 | 支持 | 已接入 mock/可选真实 Provider |
| `product_search` | 支持 | 可使用视觉摘要 | 可使用视频摘要 | 已接入 mock adapter |
| `price_compare` | 支持但无候选时可能失败 | 可接搜索结果 | 可接搜索结果 | 已接入 mock adapter |
| `render_3d` | 支持文本场景 | 可接视觉 output_ref | 可接视频理解 output_ref | 已接入 mock adapter |
| `memory_retrieval` | 支持 | 不依赖媒体 | 不依赖媒体 | 已接入 mock/local memory |
| `multi_step_orchestration` | 支持 | 支持 | 支持 | 已接入 LangGraph loop |

## 3. Text-only 能力状态

Text-only routing baseline 已完成：

- `direct_chat` 可处理普通聊天、文案和建议，不要求图片或视频。
- `image_generation` 可处理文生图请求，不要求图片或视频。
- `product_search` 可处理文本商品搜索。
- `price_compare` 可处理文本比价请求；如果没有商品候选，仍按工具错误和 RecoveryPolicy 返回结构化失败。
- `render_3d` 可处理文本场景描述。
- `memory_retrieval` 可处理“上次/之前/我喜欢”等历史指代。

覆盖测试：`tests/test_text_only_routing.py`。

## 4. Media-aware Routing 状态

Media-aware routing baseline 已完成：

- 图片 + 看图意图路由到 `image_understanding`。
- 视频 + 总结/理解意图路由到 `video_understanding`。
- 图片 + 生成意图路由到 `image_understanding -> image_generation`。
- 图片 + 搜索/比价路由到 `image_understanding -> product_search -> price_compare`。
- 视频 + 搜索商品路由到 `video_understanding -> product_search`。
- 图片/视频 + 普通聊天文本不会强制触发视觉理解。
- 缺图片/视频但请求理解图片/视频时进入 `ask_followup`。
- 歧义输入进入 `ask_followup`。

覆盖测试：`tests/test_media_aware_routing.py`。

## 5. Multi-step Routing 状态

Multi-step orchestration baseline 已完成。当前 planner 和 LangGraph loop 支持按步骤执行，并把前序结果传给后续工具。

已覆盖的多步 pattern：

- `image_understanding -> product_search -> price_compare -> image_generation`
- `memory_retrieval -> image_generation`
- `product_search -> price_compare`
- `video_understanding -> render_3d`

当前 `build_tool_input()` 会把视觉摘要传给搜索、搜索结果传给比价/生成、记忆结果传给生成、视觉 output_ref 传给 3D 渲染。

覆盖测试：`tests/test_assistant_multistep_orchestration.py`。

## 6. Eval 覆盖情况

离线 eval 已扩展为 44 条 case，覆盖以下 routing 分类：

```text
direct_chat
text_only_image_generation
text_only_product_search
text_only_price_compare
text_only_render
image_understanding
video_understanding
media_plus_generation
media_plus_search_compare
memory_retrieval
multi_step_orchestration
ambiguous_followup
```

`scripts/run_evals.py` 当前输出：

- `intent_accuracy`
- `capability_accuracy`
- `tool_selection_accuracy`
- `ordered_tool_match`
- `unexpected_tool_rate`
- `media_requirement_error_rate`
- `followup_accuracy`
- `failed_case_ids`

默认 eval 离线运行，使用 MockAdapter，不调用真实 Provider。

## 7. Vision Provider Validation 的降级定位

真实 Qwen Vision smoke 已作为 Provider validation 跑通，但它不是 Phase 5A 主线。

该验证只证明 `image_understanding` / `video_understanding` capability 可以通过可选 Provider adapter 接入真实模型。Phase 5A 的验收不依赖真实 Provider，也不要求继续扩展 Vision hardening。

后续真实 Provider 稳定化应进入单独的 Provider Hardening 阶段。

## 8. 仍然是 Mock 的能力

以下能力默认仍是 mock/local：

- `image_generation`
- `product_search`
- `price_compare`
- `render_3d`
- `memory_retrieval` 的工具实现
- 默认 `vision_understanding`

真实 Vision Provider 只允许用户显式配置环境变量并手动运行 smoke 脚本，或通过 env-gated integration tests 触发。默认 `python -m pytest` 和 `python scripts/run_evals.py` 不调用真实 Provider。

## 9. 下一阶段建议

建议下一阶段不要继续把主线改回 Vision-only。更合理的方向是：

- Provider Hardening：真实 Provider 超时、重试、成本、trace 查询和响应 schema 稳定化。
- Assistant Planner Hardening：更细粒度的 slot filling、计划解释、可选步骤策略。
- Memory Hardening：把 mock memory tool 接入持久化 store，完善检索排序和引用。
- Eval Expansion：增加更多中英文混合、歧义、多 intent、负样本 case。
- API Contract Stabilization：为 capability routing 输出明确版本化字段，减少 legacy intent 泄漏。

## 验收结果

Task 047 执行时需要运行：

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

Phase 5A 审计结论：Assistant Capability Routing Baseline 已完成。

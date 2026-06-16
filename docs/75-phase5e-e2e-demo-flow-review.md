# 75 Phase 5E E2E Demo Flow Review

## 结论

Phase 5E End-to-End Demo Flow & Response Quality 已完成。当前系统没有新增真实 Provider，没有接入真实外部 API，没有实现 MCP / Skills，也没有升级 Hybrid LLM Intent Router。默认运行路径继续使用 MockAdapter / LocalJsonAdapter，并可离线完成测试、eval 和 demo runner。

## 1. Demo Scenario Matrix 状态

已定义默认离线 demo scenario matrix：

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

当前覆盖 12 个可复现场景：

- 纯文本聊天。
- 纯文本图片生成。
- 图片理解。
- 视频理解。
- 文本商品搜索和比价。
- 图片找同款并比价。
- 商品搜索后生成图片。
- 商品搜索后进入 3D 渲染。
- 图片进入 3D 渲染。
- 结合记忆生成图片。
- 完整图片找同款、比价并生成图片。
- 歧义输入触发追问。

场景只使用 mock media id、本地小型 demo product JSON 和本地输出引用，不提交真实图片、视频、生成图、渲染产物或真实 Provider 输出样本。

## 2. Capability Output Contract 状态

核心工具结果已统一到 capability output contract：

```text
src/multimodal_agent/schemas/capability_output.py
src/multimodal_agent/schemas/tools.py
```

已覆盖的能力包括：

- direct_chat
- image_generation
- image_understanding
- video_understanding
- product_search
- price_compare
- render_3d
- memory_retrieval
- memory_save

API response、WebSocket 事件、response composer 和 eval 均可读取结构化 contract。contract 构建会过滤敏感字段，避免输出 API Key、Authorization、Bearer token、base64 和 raw provider response。

## 3. Response Composer 改进状态

当前 response composer 使用模板式离线实现，不调用 LLM：

```text
src/multimodal_agent/agent/response_templates.py
src/multimodal_agent/agent/response_composer.py
```

已支持：

- direct_chat 保留 chat adapter 产出的文本回复。
- 单工具能力生成具体摘要，不再普遍落到“已完成请求处理。”。
- 多工具任务按工具执行顺序总结。
- 商品搜索、比价、图片生成、3D 渲染、记忆检索和视觉理解有能力相关文案。
- partial failure 和 follow-up 场景输出可解释信息。

当前没有引入 LLM response composer，后续可在 Phase 5F 之后再考虑。

## 4. Eval Suite 分层状态

Eval suite 已按 `suite` / `category` 分层：

```text
tests/evals/eval_cases.json
scripts/run_evals.py
```

支持：

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite routing
python scripts/run_evals.py --suite e2e
```

默认 eval 离线运行，不调用真实 Provider。当前 summary 包含全局指标和 suite-level summary，保留 `failed_case_ids`，并包含 response quality pass rate。

## 5. E2E Demo Runner 状态

已新增默认离线 demo runner：

```text
scripts/run_demo_flows.py
```

支持：

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario product_search_compare
```

每个 scenario 输出：

- `scenario_id`
- `status`
- `tool_sequence`
- `response_text`
- `errors`
- `run_id`
- `trace_id`

Runner 显式使用默认 `ProviderConfig()`，不会读取用户 shell 中的真实 Provider 环境变量，因此默认不会触发外部调用。

## 6. 默认 Mock/Local 安全边界

默认路径保持以下边界：

- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- 默认 demo runner 不调用真实 Provider。
- 默认 Product Search / Price Compare 使用 MockAdapter 或 LocalJsonAdapter。
- 默认 Render 使用 MockRenderAdapter。
- 默认 Image Generation 使用 mock/local 输出引用。
- 默认 Vision Understanding 使用 MockVisionUnderstandingAdapter。

真实 Provider 仍只能通过用户显式配置环境变量，并手动运行 smoke 脚本或启用 env-gated integration tests 触发。

## 7. 仍然是 Mock 的能力

以下能力目前仍是 mock/local baseline，不是生产真实能力：

- direct_chat 默认是 mock chat adapter。
- image_generation 默认返回本地 mock 输出引用。
- image_understanding / video_understanding 默认使用 mock 视觉理解。
- product_search 默认使用 mock 或本地小型 JSON。
- price_compare 默认使用 mock/local 比价。
- render_3d 默认返回 mock render preview。
- memory 默认使用 in-memory/local store。

这些 mock/local 能力用于验证 Agent 编排、contract、API、eval 和 demo flow，不应被描述为真实生产 Provider 成功。

## 8. Phase 5F 建议

Phase 5F 可以考虑以下方向，但不应回填到 Phase 5E：

- Hybrid Intent Router / Planner Quality：在保持 rule-based baseline 的前提下，引入 env-gated 或 mockable LLM router 评估。
- Provider Safety / Retry / Cost / Trace Query：强化真实 Provider 的超时、重试、成本预算、trace 查询和敏感信息过滤。
- Memory Hardening：持久化策略、检索质量、用户偏好合并和过期策略。
- MCP / Skills Packaging：把稳定能力打包为可复用 skill 或 MCP，但不改变当前默认离线测试边界。

## 验收快照

本审计任务的验收命令：

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

验收结果以任务执行输出为准。

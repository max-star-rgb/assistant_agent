# 39 真实 Vision Provider Smoke Runbook

## 目标

本 runbook 用于手动验证真实 Vision Provider 的最小链路。默认测试和默认运行仍使用 MockAdapter；只有你在本地显式设置环境变量并运行 smoke 脚本时，才会尝试调用真实 Provider。

不要把 API Key、`.env.local`、真实图片或视频提交到仓库。

## 1. 准备低风险图片

把本地低风险图片放入：

```text
demo_data/images/
```

推荐样例：

- 鞋子、背包、杯子、桌面商品图。
- 无人的室内场景图。
- 公开样例图。

避免使用：

- 身份证、合同、票据、车牌、人脸。
- 家庭照片、公司内部资料、客户数据。
- 任何不希望发送给外部 Provider 的内容。

示例路径：

```text
demo_data/images/shoe.jpg
```

## 2. 设置环境变量

OpenAI 示例：

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=openai
export OPENAI_API_KEY="<set-in-local-shell>"
export OPENAI_VISION_BASE_URL="https://api.openai.com/v1"
export OPENAI_VISION_MODEL="gpt-4o-mini"
```

Qwen 示例：

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
export QWEN_VISION_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export QWEN_VISION_MODEL="qwen-vl-plus"
```

可以参考 `.env.example` 的变量名，但不要把真实 key 写入 `.env.example`。如果使用 `.env.local`，确保它保持本地未提交。

`*_VISION_BASE_URL` 使用 OpenAI-compatible base URL，例如以 `/v1` 结尾；代码会自动调用 `/chat/completions`，也兼容已经填完整 `/chat/completions` 的地址。

## 3. 确认默认测试仍离线

先运行默认测试：

```bash
python -m pytest
```

默认 pytest 不应调用真实 Provider。Integration tests 默认 skip；只有显式设置 `RUN_INTEGRATION_TESTS=1` 时才会进入真实集成测试路径。

## 4. 运行 smoke 脚本

确认图片存在后，显式运行：

```bash
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

可选自定义问题：

```bash
python scripts/smoke_real_vision.py \
  --image demo_data/images/shoe.jpg \
  --question "请描述图片中的主要物体、颜色、材质和场景。"
```

脚本只在命令行显式执行时运行；import 脚本不会调用真实 Provider。

## 5. 查看 response / trace / errors

成功时输出 JSON，重点查看：

```text
status
provider
intent
response_text
tool_calls
vision_result
errors
run_id
trace_id
```

`status` 为 `success` 表示 AgentGraphRuntime 跑通；`trace_id` 可用于后续定位 graph trace。

真实 provider 成功时，`tool_calls[].output_ref` 应为 `provider://vision/{provider}`，例如 `provider://vision/qwen`。如果 provider 是 `qwen` 但输出仍是 `mock://vision/white-low-top-sneaker`，不能视为真实 Provider 成功。

`vision_result` 应包含结构化视觉结果，至少包括 `summary`，并尽量包含 `objects`、`colors`、`materials`、`scene`、`style_tags`、`text_in_media`。

更完整的成功判定见 `docs/48-real-vision-smoke-success-runbook.md`。

失败时优先查看：

```text
errors[].code
errors[].message
errors[].detail
```

脚本不会打印 API Key。

## 6. 常见错误

`provider_unconfigured`

- `MULTIMODAL_AGENT_VISION_PROVIDER` 未设置为 `openai` 或 `qwen`。
- 对应 API Key 缺失，例如 `OPENAI_API_KEY` 或 `QWEN_API_KEY`。

`provider_timeout`

- Provider 请求超时。
- 网络不可达、代理配置问题或 Provider 服务繁忙。
- 可以稍后重试，或检查 base URL。

`provider_bad_response`

- Provider 返回格式与当前 `VisualUnderstandingResult` schema 不匹配。
- 可能需要后续调整真实 Provider response mapping。

`missing_demo_image`

- `--image` 指向的本地文件不存在。
- 确认图片路径和文件名。

## 7. 清理本地敏感配置

运行后可以清理当前 shell 中的 key：

```bash
unset OPENAI_API_KEY
unset QWEN_API_KEY
unset MULTIMODAL_AGENT_VISION_PROVIDER
```

提交前检查：

```bash
git status --short
```

不要提交：

- `.env`
- `.env.local`
- 真实图片或视频
- 包含 API Key 的日志或输出

## 8. 下一步

如果 smoke 跑通，再进入 Phase 4.5 的审计任务；是否进入 Phase 5 应根据 smoke 结果和真实问题决定。

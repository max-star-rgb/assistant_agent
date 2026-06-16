# 116 Phase 6A：Local Demo Entry / CLI

## 目标

提供一个最小本地用户入口，让用户无需理解内部 tasks/docs，也能运行 Assistant Agent。

## 核心产物

```text
scripts/run_assistant_cli.py
scripts/run_demo_flows.py 已增强
docs/quickstart-local.md
docs/demo-cli.md
```

## CLI 应支持

```text
文本输入
可选 image_ref
可选 video_ref
可选 scenario_id
显示 response_text
显示 tool_sequence
显示 run_id / trace_id
显示 errors
```

## 示例命令

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python scripts/run_assistant_cli.py --text "生成一张日系极简海报"
python scripts/run_assistant_cli.py --scenario product_search_compare
```

## 默认行为

- 使用 MockAdapter / LocalJsonAdapter。
- 不调用真实 Provider。
- 不需要 API Key。
- 不要求真实图片/视频。
- 输出 JSON 或 readable text。

## 验收标准

- CLI 可以运行至少 5 个核心场景。
- CLI 输出不是“已完成请求处理”这种无信息结果。
- CLI 输出 run_id / trace_id。
- 默认离线。

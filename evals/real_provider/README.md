# Real Provider Evals

本目录保存真实 chat provider 的 Agent 行为评测集。它不是 pytest scope，也不属于默认离线测试。

默认设计是：真实 LLM provider + 受控 mock/local tool world。这样可以评估模型在完整
`AgentGraphRuntime` 中的工具选择、步骤、失败处理和最终回答，同时避免天气、日历、搜索等外部
工具状态让结果不可复现。

运行示例：

```bash
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke \
MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek \
DEEPSEEK_CHAT_API_KEY=... \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_real_provider_evals.py
```

只预览 case，不调用 provider：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_real_provider_evals.py --dry-run --max-cases 5
```

输出目录：

```text
.data/evals/real_provider/<timestamp>_<suite>_<provider>_<model>/
  summary.json
  results.jsonl
  traces.jsonl
  cases.json
```

当用户基于真实评测结果追问失败原因时，先读上述 `.data` 机器日志，再结合源码回答。

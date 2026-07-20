# 最小测试安全网

本目录只保留由真实风险驱动的默认离线安全网。它不承担覆盖率证明、模块枚举、实现细节验证或
第三方框架验证，也不按源码目录建立镜像 scope。

默认命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

当前安全网保护以下稳定边界：

- 包与默认离线 runtime 可以初始化；
- 普通文本 run 可以完成并形成终态事件；
- Agent system prompt 保持通道无关，不混入电话、TTS 或 WebSocket 规则；
- provider-native tool call 可以经过治理链路并返回最终回答；
- 主 LLM 超时会形成可终止的重试响应；
- cancel token 会终止 run；
- `LLMEvent -> AgentEvent -> RealtimeAgentEvent -> Gateway frame` 的核心转换可用；
- session、run 与 memory 按用户身份隔离。

`tests/evals/eval_cases.json` 是 `scripts/run_evals.py` 的离线评测数据，不属于 pytest。
真实 Provider、付费 API 和外部服务验证使用显式 operator smoke/pilot 脚本，不进入默认 pytest。

新增或修改 pytest 必须遵循根目录 `AGENTS.md` 的 `Testing Policy`。不要创建新测试文件，除非
现有模块无法合理承载一个真正独立的稳定边界。

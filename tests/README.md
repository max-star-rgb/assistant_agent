# 测试策略与最小安全网

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

`tests/tools_plugin/test_*_plugin.py` 是例外的显式 opt-in 插件装配测试：它们读取真实 Provider/MCP
配置并构造真实 adapter，但不发起外部调用。默认 pytest 会跳过这些用例；需要验证本机真实配置时运行：

```bash
ASSISTANT_AGENT_RUN_REAL_TOOL_PLUGIN_TESTS=1 \
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tools_plugin/test_*_plugin.py
```

未配置某项可选能力时，对应插件测试会单独 skip；主 chat Provider 配置不完整则直接失败，避免把
错误的 mock 配置误报为真实 Provider 验证通过。`tests/test_tool_plugin_runtime.py` 始终使用 mock，
验证插件 Tool 经 `AgentGraphRuntime` 治理链路完成一次原生 tool-call 闭环。

## Testing Policy

本项目采用风险驱动测试，而不是覆盖率驱动测试。不要因为实现了新代码，就自动新增 pytest。

只有出现以下情况时，才新增或修改测试：

1. 发现了真实 bug，需要最小回归测试；
2. 新增或修改了稳定、可观察的外部行为或协议契约；
3. 修改了并发、取消、超时、重试、状态机、持久化兼容性、事件顺序或身份隔离等高风险机制；
4. 修改了关键主链路，并且现有安全网无法发现该链路的严重故障。

重命名、移动文件、行为不变的内部重构、日志、注释、文档、简单 wrapper、私有实现细节、
第三方框架自身行为以及没有真实风险依据的假设性边缘场景，默认不新增测试。

## 测试文件组织

测试文件按稳定行为边界组织，不按“文件数量最少”组织，也不按源码目录机械镜像：

- `tests/test_safety_net.py` 只承载跨层启动、核心运行闭环和少量全局不变量，不是所有新测试的默认落点；
- 现有测试已经覆盖同一个行为边界，且新增场景共享相同入口、fixture 和失败语义时，扩展现有文件；
- 新测试属于独立契约、独立故障域或需要不同 fixture 时，创建聚焦命名的新测试文件；
- 一个测试文件开始混合多个无关领域，或失败时无法从文件名和测试名判断责任边界时，应拆分而不是继续追加；
- 不得为了“优先修改已有测试”而机械地把新场景堆进 `test_safety_net.py`；也不得为了目录整齐而给每个源码文件创建对应测试文件。

新增测试前先搜索现有测试，确认没有重复覆盖，并选择最稳定、最低成本的可观察边界。测试应
验证外部行为、状态转换、事件、持久化结果和副作用，不以项目私有方法调用次数作为主要断言。
外部边界优先使用 reusable fake 或 in-memory adapter。

默认测试不得访问远程或付费服务；外部集成验证必须显式 opt-in。不得新增或提高覆盖率门槛。
修改行为时应同步删除或合并已经冗余的测试。

## 任务汇报

每次任务结束时明确写出以下之一：

- `Tests: existing tests were sufficient.`
- `Tests: updated <test name> because <observable behavior changed>.`
- `Tests: added <test name> as a regression for <specific bug>.`
- `Tests: not added because the change does not affect observable behavior or a high-risk boundary.`

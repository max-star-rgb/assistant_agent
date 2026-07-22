# 测试分层与最小安全网

本文件是 pytest 测试分层、目录归属、默认收集、测试决策、验证范围和任务汇报的唯一权威；
`AGENTS.md` 只提供导航，skill 只提供 workflow 入口，不复制本文件规则。

测试按运行目的分为三个目录，不再将不同生命周期的用例平铺混放：

```text
tests/
  critical/      # 基础必要测试；裸 pytest 默认收集
  feature/       # 功能开发验证；稳定后仅按需运行
  tools_plugin/  # 用户显式触发的真实 Provider/MCP 调用
```

`critical` 只保留由真实风险驱动、需要长期守住的默认离线安全网。它不承担覆盖率证明、模块枚举、
实现细节验证或第三方框架验证，也不按源码目录建立镜像 scope。

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
- Agent-Service interrupt 会取消活动 turn、抑制旧输出并保持连接；
- 入口超时仍保留 trace correlation 和可诊断的部分 trace；
- `LLMEvent -> AgentEvent -> RealtimeAgentEvent -> Gateway frame` 的核心转换可用；
- session、run 与 memory 按用户身份隔离；
- 跳过自动 memory read 时不会访问持久化 store；
- Tool schema、catalog、validation、confirmation 和 execution 保持治理边界。

`feature` 保存某次功能实现期间有价值、但功能稳定后不需要在每次普通开发中重复执行的 pytest。
它们不会被裸 `pytest` 收集；修改对应功能、排查相关回归或准备较宽验证时，显式指定文件或目录：

- `test_runtime_provider_streaming.py`：Provider 原生 streaming 功能验证；
- `test_tool_plugin_runtime.py`：mock plugin Tool 的跨层 runtime 装配闭环。

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/feature
```

不要因为用例已进入 `feature` 就假设它必须永久保留。对应功能稳定且不再提供独立风险证据时，
可以在后续相关变更中删除；若它保护的是稳定外部契约、具名缺陷或高风险机制，则应移入
`critical`，而不是长期留在模糊的中间状态。

Agent 行为 eval 数据统一放在根目录 `evals/`，格式、分层和运行方式见 `evals/README.md`，不属于
pytest。真实 Provider、付费 API 和外部服务验证使用显式 operator eval/smoke/pilot 脚本，不进入默认
pytest。

`tests/tools_plugin/test_*_plugin.py` 只保存用户显式触发的真实 Provider/MCP 调用。这里禁止 mock、
只装配不调用、用 skip 把未配置能力伪装成通过，或者由程序、agent、CI、定时任务和默认 pytest 自主
启用。只有用户在当前任务中明确要求运行真实工具测试后，才允许人工执行带专用命令行开关的命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  -s --run-real-tools-plugin tests/tools_plugin
```

仅设置环境变量不能启用该目录；显式指定目录但缺少 `--run-real-tools-plugin` 时测试必须失败。真实工具
必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`，并把 Provider 的真实结果输出到本次
测试终端；Provider 未配置、认证失效、超时、限流、响应无效或返回失败时，测试必须明确失败，不能
skip、回退 mock 或伪造成功。默认先收录无副作用的只读 smoke；任何付费、写入或危险工具都必须有
独立具名测试、确定性输入和安全清理策略，并由用户在当次任务中明确要求后才能运行。

`tests/feature/test_tool_plugin_runtime.py` 始终使用 mock，验证插件 Tool 经 `AgentGraphRuntime` 治理链路
完成一次原生 tool-call 闭环；真实 Provider 装配本身由启动 fail-closed 和上述真实调用共同验证，不在
`tests/tools_plugin` 保留只装配测试。

## 测试策略

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

- `tests/critical/test_safety_net.py` 只承载跨层启动、核心运行闭环和少量全局不变量，不是所有新测试的默认落点；
- 新用例先判断其生命周期：长期必要边界进入 `critical`，开发期功能验证进入 `feature`，真实外部配置验证进入 `tools_plugin`；
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

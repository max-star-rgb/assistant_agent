# pytest 测试体系

本文件是 pytest 目录归属、默认收集、测试决策和任务汇报的唯一权威。真实 Provider、真实外部服务和
模型行为质量不属于 pytest，统一进入根目录 `evals/`。

## 目录

```text
tests/
  unit/          # 单一纯逻辑边界；当前没有需要独立保留的用例时可以不存在
  integration/   # 多个项目模块协作，使用 mock/fake/in-memory/local adapter
  contract/      # schema、协议、事件、治理和稳定外部契约
```

目录第一层表达测试性质，第二层表达故障域，例如：

```text
tests/integration/context/
tests/integration/memory/
tests/integration/runtime/
tests/integration/tools/
tests/contract/gateway/
tests/contract/observability/
tests/contract/tools/
```

不要按源码文件机械镜像目录，也不要为单个测试创建一层目录。

## 三层边界

### unit

只验证一个窄逻辑单元，不启动 Runtime，不访问数据库、网络或进程。外部依赖应当不存在或完全替换。

### integration

验证多个项目模块的协作和可观察行为。允许启动 `AgentGraphRuntime`、Gateway、FastAPI TestClient、
in-memory store、scripted chat adapter 和本地 fake，但不得访问真实 Provider、付费 API、真实 MCP
或真实 Memory 服务。

### contract

验证需要长期守住的稳定边界，例如协议 frame、事件映射、Tool schema、validation、confirmation、
身份隔离和 trace correlation。契约测试可以跨少量模块，但断言重点必须是稳定协议或状态，而不是私有
实现调用次数。

## 默认执行

所有 pytest 都必须离线安全，因此裸 pytest 收集整个 `tests/`：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

定向运行示例：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/memory

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/contract/tools
```

pytest 中禁止：

- 真实 chat Provider；
- 真实 MCP、Mem0、Google Calendar、天气或搜索服务；
- 仅因未配置外部能力而 `skip`；
- 检测到本机 key 后自动启用真实调用；
- 从 real 静默回退到 mock 并报告通过。

真实系统能力验证见 `evals/system/`；Langfuse 案例评估见 `evals/cases/langfuse/`。

## 测试策略

项目采用风险驱动测试，而不是覆盖率驱动测试。只有出现以下情况时才新增或修改测试：

1. 发现真实 bug，需要最小回归测试；
2. 新增或修改稳定、可观察的外部行为或协议契约；
3. 修改并发、取消、超时、重试、状态机、持久化、事件顺序或身份隔离等高风险机制；
4. 修改关键主链路，且现有测试无法发现其严重故障。

文档、日志、注释、行为不变的移动或重命名、简单 wrapper、第三方框架自身行为和没有风险证据的
假设性边缘场景，默认不新增测试。

新增用例前先搜索现有测试。相同入口、fixture 和失败语义应扩展现有文件；独立契约或故障域应创建
聚焦命名的文件。一个文件混合多个无关领域时应拆分。

## 断言原则

优先断言：

- 结构化状态和终态；
- schema、协议字段和事件类型；
- Tool validation/execution 结果；
- 持久化结果和副作用；
- 身份隔离、调用顺序和错误分类。

普通 pytest 不断言完整自然语言回复、整段 prompt、Tool description 或控制台输出。验证内容透传时
使用无语义 sentinel；只有文本本身是稳定外部契约时才做聚焦文本断言。

外部边界优先使用 reusable fake、scripted adapter 或 in-memory implementation，不以私有方法调用次数
作为主要正确性证据。

## 与 Eval 的转换

- system eval 发现确定性代码缺陷后，应补充最小 pytest regression；
- pytest 通过但真实模型选择或任务质量不稳定时，问题留在 eval，不写成随机 pytest；
- 真实 Provider/MCP 直调 smoke 也属于 `evals/system/`，不得重新放回 `tests/`。

## 任务汇报

每次开发任务结束时明确写出以下之一：

- `Tests: existing tests were sufficient.`
- `Tests: updated <test name> because <observable behavior changed>.`
- `Tests: added <test name> as a regression for <specific bug>.`
- `Tests: not added because the change does not affect observable behavior or a high-risk boundary.`

同时列出实际执行的 pytest 命令。若本轮调用真实 Provider，还必须另外报告 system/case eval 的范围和
结果。

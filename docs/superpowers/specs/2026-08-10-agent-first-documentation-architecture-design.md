# Agent-first 项目文档架构设计

日期：2026-08-10  
状态：已实施（阶段一）

## 背景

本项目文档的首要消费者是 coding agent，而不是从头阅读手册的人。当前仓库已经建立
`AGENTS.md -> 专项权威文档 -> 源码和测试` 的基本层级，但仍存在三个问题：

1. 根级权威文档合计约 5,600 行，最长单篇超过 1,100 行；Agent 为定位少量约束需要加载大量叙述；
2. 文档 owner、适用源码范围和验证入口主要依赖自然语言，无法可靠检查路由遗漏和跨文档复制；
3. 命令、环境变量、URL、协议字段等精确事实容易同时出现在 authority、索引和 skill 中，修改时产生漂移。

本设计把文档从“阅读材料集合”改造成“Agent 的任务路由与约束加载系统”。

## 目标

- 普通单领域任务只需读取 `AGENTS.md`、机器路由清单和一篇领域 authority；
- 每项稳定契约只有一个 owner，其他文档只保留边界说明与链接；
- Agent 能根据任务和改动路径确定必读 authority、需要复核的 authority 和验证命令；
- 精确配置与协议字面量的错误复制可以被离线检查发现；
- 不增加运行时依赖，不调用真实 Provider，不改变产品运行行为；
- 允许现有文档渐进迁移，不要求一次性重写全部历史材料。

## 非目标

- 不为人类重新编写一套平行手册；
- 不从 Markdown 生成源码、环境变量或运行配置；
- 不要求每次源码变化都机械修改文档；
- 不按篇幅机械拆分文档；只有稳定、独立拥有者和验证入口同时存在时才拆分 authority；
- 不批量删除 `docs/development/**`、`docs/superpowers/**` 或 `docs/interview/**`；
- 不把 skill、README、历史 spec 或 roadmap 提升为当前事实权威。

## 信息架构

```text
AGENTS.md
  启动约束与任务类型路由
        |
        v
docs/authority.toml
  domain owner、适用范围、必读文档、验证入口和排他事实
        |
        v
领域 authority Markdown
  不变量、边界、入口、失败语义和运维决策
        |
        v
源码 / schema / CLI --help / tests
  可执行事实与最终裁决

docs/development、docs/superpowers、docs/interview
  历史或专项材料；不参与默认加载
```

### `AGENTS.md`

`AGENTS.md` 继续作为唯一启动入口，只保存：

- 全局工作、安全和架构硬边界；
- 简短的任务类型路由；
- authority manifest 的位置和使用规则；
- 文档与测试的通用职责边界。

它不复制领域不变量、配置字段、命令参数或操作流程。现有路由表在迁移期保留，validator 检查其中的
authority 路径能映射到 manifest；待 manifest 覆盖稳定后，再决定是否缩短路由表，不自动生成
`AGENTS.md`。

### `docs/authority.toml`

选择 TOML 而不是 YAML：项目默认 Python 可用标准库 `tomllib` 解析，无需增加 PyYAML 依赖。manifest
只描述路由和所有权，不复制领域正文。

建议 schema：

```toml
schema_version = 1
coverage = "pilot"

[[domains]]
id = "agent-eval"
authority = "evals/README.md"
read_when = ["Agent Task eval", "Langfuse Experiment"]
source_globs = [
  "evals/agent/**",
  "src/assistant_agent/evaluation/**",
]
thin_references = [
  "scripts/README.md",
  "docs/observability-harness.md",
]
verification = [
  "python scripts/check_documentation_authority.py",
]
exclusive_literals = [
  "assistant-agent-eval-webhook",
  "ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET",
]
exclusive_allowlist = []
```

字段语义：

- `id`：稳定且唯一的领域标识；
- `authority`：该领域唯一当前权威；
- `read_when`：供 Agent 判断是否加载的短语义标签，不作为关键词路由产品请求；
- `source_globs`：用于代码变更后的文档复核提示，不表示源码所有权或强制文档修改；
- `thin_references`：允许提及领域并链接 authority、但不得复制排他事实的入口文档；
- `verification`：该领域文档或实现变化后的离线验证入口；
- `exclusive_literals`：只能出现在 owner authority、源码、测试或明确豁免文件中的精确配置和协议字面量。
- `exclusive_allowlist`：排他事实确需出现在另一篇当前文档时使用的精确文件白名单；不得用目录级宽泛
  豁免掩盖复制；
- `coverage`：`pilot` 时只要求 manifest 中的 authority 已由 `AGENTS.md` 路由；全部根级 authority
  登记后改为 `complete`，此时还反向检查 `AGENTS.md` 当前路由没有漏登 manifest。

manifest 不保存 API key、真实 URL、用户数据、动态运行状态或详细操作正文。

### 领域 authority Markdown

现有 authority 保持原路径，先统一增加一个紧凑契约卡片，不立即重写全文：

```text
定位
Owns
Does not own
必须保持的不变量
源码与 schema 入口
运行或验证入口
相邻 authority
```

正文只记录无法安全地从单个源码入口推导的跨模块约束、设计边界、失败语义和 operator 决策。以下内容
优先引用可执行来源，不在多篇 Markdown 手抄：

- CLI 参数：`--help`；
- API 字段：Pydantic model/OpenAPI；
- 环境变量：settings model 或集中常量；
- Tool catalog：registry/spec；
- 测试分层与命令：对应测试 authority；
- 当前实现状态：源码和验证结果。

篇幅不是拆分依据。如果一篇 authority 同时包含两个稳定 owner、两组独立源码范围和两套验证入口，才
提出拆分；否则通过契约卡片、章节重排和删除重复叙述压缩。

### 薄入口与 specialty skill

- `README.md` 面向人类快速导航；
- `scripts/README.md` 只列命令用途、主要副作用和 authority 链接；
- `tests/README.md` 与 `evals/README.md` 可作为各自目录的专项 authority；
- `.codex/skills/**/SKILL.md` 只保存 workflow、门禁和读取顺序，必须路由到 authority；
- 历史 plan/spec 允许保留当时方案，但不参与当前一致性判定，也不因正文过期自动删除。

## Validator 与证据采集

新增稳定离线入口 `scripts/check_documentation_authority.py`，使用标准库解析 TOML，并复用或抽取现有
documentation evidence collector 的路径和 Markdown 链接能力。

### 阻断性检查

以下问题返回非零退出码：

- manifest schema/version 不支持；
- domain ID、authority 或排他事实定义重复；
- authority、thin reference 或验证入口路径不存在；
- manifest authority 未出现在 `AGENTS.md` 路由；`coverage=complete` 时还检查反向遗漏；
- `exclusive_literals` 出现在 owner、源码、测试和精确 allowlist 之外的当前 Markdown/skill 中；历史
  plan/spec、development 和 interview 材料不参与当前排他事实检查；
- 当前 authority 的相对 Markdown 链接失效；
- source glob 无法匹配任何受版本控制路径，且未声明为预留范围。

### 非阻断复核提示

validator 接受可选 Git range 或使用 dirty path 列表，将改动文件匹配到 `source_globs`，输出
`review_required` domain。它只要求复核，不要求 authority 必须同时产生 diff，避免无意义文档 churn。

以下情况仅报告，不自动修改或删除：

- 一项源码变化可能命中多个 domain；
- authority 超长或存在疑似重复语义；
- 历史文档仍引用旧名称；
- 一个候选文档可能已经失去独有价值。

### 稳定验证

文档 authority 的结构合法性由稳定 validator 命令和 `AGENTS.md` 完成门禁保护：manifest 可解析、路径
存在、路由一致、排他事实没有泄漏到薄入口。它属于仓库治理而非产品框架 invariant；根据
`tests/README.md`，不新增或修改 `tests/core`。实现期使用可删除的
`tests/tdd/documentation-authority` 做 RED/GREEN。语义内容和“是否真的需要改文档”不交给机械测试
裁决。

documentation-sync skill 在全量审计时依次运行：

1. authority validator；
2. 现有 evidence collector；
3. specialty skill validation；
4. 相关离线测试与 `git diff --check`。

## Agent 加载流程

Agent 开始仓库任务时：

1. 读取 `AGENTS.md`；
2. 根据明确任务类型和将要触及的源码路径查询 manifest；
3. 加载匹配 domain 的 authority；
4. authority 指向第二领域且任务确实跨边界时，才加载第二篇；
5. 从 authority 指向源码、schema、测试或 CLI 获取精确事实；
6. 完成前运行 domain verification，并根据 diff 输出复核 owner。

`read_when` 仅帮助 coding agent 选择工程文档，不进入产品 Runtime，也不能用来判断终端用户请求、选择
Tool 或预选 Agent workflow。

## 首批试点

第一阶段只登记两个边界清楚、刚发生过漂移的 domain：

### `runtime-observability`

- authority：`docs/observability-harness.md`；
- owns：canonical trace、OTel/Langfuse 投影、runtime audit、Live Observation Rule；
- does not own：Dataset、Agent Task、Remote Experiment、Experiment Score 完整性。

### `agent-eval`

- authority：`evals/README.md`；
- owns：eval 分层、Task/Environment、Dataset 同步、Experiment、Remote webhook、task-level Score；
- does not own：生产 Trace 生命周期、日常 runtime audit、Live Observation Rule 配置。

`scripts/README.md` 是两者共同的薄入口，不成为 domain authority。

## 渐进迁移

### 阶段一：基础设施与试点

- 创建 manifest 和 validator；
- 登记 `runtime-observability` 与 `agent-eval`；
- 为两篇 authority 增加契约卡片；
- 把现有 webhook 排他字面量检查迁入 validator；
- 用 `tests/tdd/documentation-authority` 完成 validator 的临时 RED/GREEN；
- 将 validator 登记为 `AGENTS.md` 中相关文档和路由变更的完成检查。

### 阶段二：覆盖当前根级 authority

逐个登记 Gateway、runtime stream、tool calling、memory、context、multimodal embedding、media 和
multi-agent。每次只处理一个 domain，先映射源码和验证入口，再压缩薄入口，不批量移动文件。

### 阶段三：内容压缩与历史清理

依据真实 Agent 任务中的加载成本和重复证据，重排或拆分超长 authority。历史文档只有在替代 authority
存在、无独有运维/API/兼容价值、入链引用已修复且源码/测试证据充分时才删除；否则只列为候选。

## 失败与兼容处理

- manifest 无效时，Agent 回退到现有 `AGENTS.md` 路由，但验证命令失败，不把缺失 owner 静默忽略；
- source path 同时命中多个 domain 时全部报告，由任务上下文决定实际必读范围；
- 排他字面量确需出现在第二文档时，必须在 manifest 中按具体文件显式豁免并说明理由；
- validator 不联网、不读取 `.env`，不调用 Provider，不改写 Markdown；
- manifest schema 升级必须保留显式版本错误，禁止猜测兼容；
- 试点期间保留现有 AGENTS 路由，避免 manifest 工具故障阻断基本开发。

## 成功标准

- eval 或 runtime audit 单领域任务默认只需加载一篇 authority；
- webhook URL、签名环境变量和请求 envelope 只存在于 eval authority；
- `scripts/README.md` 不再承载领域状态机或配置协议；
- 修改试点源码时 validator 能报告对应 domain；
- 删除/重命名 authority、断开链接或复制排他字面量时离线检查失败；
- 默认检查保持 mock/local/offline，无新增依赖；
- 迁移不改变 Runtime、Experiment、Provider、Tool 或 Memory 行为。

## 预期实现范围

后续实施预计涉及：

- 新建 `docs/authority.toml`；
- 新建 `scripts/check_documentation_authority.py`；
- 修改 `AGENTS.md`，加入 manifest 使用规则但保持其为薄路由；
- 修改 `docs/observability-harness.md` 与 `evals/README.md`，增加契约卡片；
- 修改 `scripts/README.md`，索引 validator；
- 更新 `.codex/skills/assistant-agent-documentation-sync`，让 collector/validator 串联；
- 新增 `tests/tdd/documentation-authority` 临时测试，不修改 `tests/core`。

第一阶段不迁移其他 authority，不删除历史文档，也不修改产品代码。

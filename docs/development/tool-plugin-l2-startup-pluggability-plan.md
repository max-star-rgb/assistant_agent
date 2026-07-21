# Tool 插件 L2 启动时可插拔实施计划

状态：已实施（2026-07-21）

创建时间：2026-07-21

目标级别：L2（配置化发现，重启生效，运行期间 Registry 不变）

## 1. 背景与结论

当前 Tool 架构已经完成 L1：内置能力按 `tools/plugins/<capability>/` 隔离，各
`ToolPlugin.build_tools(context)` 负责本领域 Provider readiness、Adapter/Service 创建和 Tool 实例化，
`ToolRegistry` 只接收已构造 Tool，并负责契约投影、查找和执行。

当前仍不是启动时可插拔系统：

- 默认内置插件由 `tools/plugins/defaults.py` 显式 import 和枚举；新增能力包仍需修改该文件；
- `ToolPlugin` 只有 `plugin_id` 和 `build_tools()`，没有 API version、插件版本、来源和构造报告；
- `ToolRegistry` 只保存 `tool_name -> Tool`，不知道 Tool 归属哪个插件；
- 显式本地 Tool loader、MCP Tool 注册与内置插件装配是三条独立路径；
- Registry 支持逐个 `register()`，没有完成装配后的只读边界；
- 少数 Tool 的可信输入绑定、模型输入投影和 observation 仍由核心代码按 Tool 名称处理。

本阶段实现 L2，而不是直接进入 L3。插件在进程启动时从受信任内置集合和显式配置的 Python module
中发现、校验、构造并一次性注册；启动完成后 Registry 不再改变。配置变化通过重启生效。

## 2. 目标

- 普通外部/可选 Python Tool 插件可以通过显式 module 配置接入默认 runtime，无需修改
  `registry.py`、`defaults.py`、`tool_ids.py` 或 executor。
- 内置插件和配置插件使用同一份描述、校验、构造、冲突检查和装配报告协议。
- 保留内置插件的显式可信清单；不通过扫描目录扩大默认可信代码面。
- Registry 记录每个 Tool 的 `plugin_id`、插件版本和来源，支持诊断与 trace 安全投影。
- 一批插件的 Tool 必须先完整校验，再原子提交；单插件部分注册不得污染最终 Registry。
- 默认 runtime 装配完成后 Registry 进入只读状态，单个 run 继续通过 `RunToolCatalog` 获得稳定的
  run-scoped 暴露集合。
- mock 模式继续强制所有 Provider-backed Tool 使用 mock；real 模式只注册配置完整的真实实现，禁止
  静默 fallback。
- 所有 Tool 继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`，插件发现不得授予执行
  权限或绕过 exposure、确认、Schema、身份和审计边界。
- 为以后 L3 保留 `plugin_version`、Registry generation 和 ownership 数据，但本阶段不实现运行时切换。

## 3. 非目标

- 不实现运行时安装、启用、禁用、替换或卸载插件。
- 不实现活动调用 drain、插件 `start/stop`、热更新回滚或多 generation 并存。
- 不扫描 `tools/plugins/`、当前工作目录、用户目录或任意 Python package。
- 不因为发现了 Python entry point 就自动信任或启用第三方代码；首版不引入 entry point discovery。
- 不把 MCP server 生命周期伪装成进程内 Python 插件生命周期。
- 不要求所有跨层 Tool 都做到“只改插件目录”；稳定 API capability、durable 协议、专用 UI 或新的可信
  输入来源仍需显式修改对应公共契约。
- 不在本阶段重写 legacy planner、intent/router 或删除所有按 Tool 名称的产品特例。
- 不增加依赖，不调用真实 Provider。

## 4. 术语和信任边界

### 4.1 插件来源

L2 支持两类插件来源：

| 来源 | 发现方式 | 默认信任 | 典型用途 |
| --- | --- | --- | --- |
| `builtin` | 仓库内显式 `builtin_tool_plugins()` | 受信任 | core、memory、vision、shopping 等产品内置能力 |
| `configured_module` | 环境变量/本机配置中显式列出的 importable module | 仅在 operator 显式启用后信任 | 仓库外或部署可选的 Python Tool 插件 |

MCP Tool 保持外部 Tool source：它由显式 server 配置、per-tool allowlist 和 MCP adapter 发现。装配报告
可以统一记录 `source_type=mcp`，但 MCP 不实现 `ToolPlugin`，避免混淆进程内代码加载和远端代理协议。

现有 `tools.loader` 的 `__assistant_tools__` 模块契约保留给 workflow skill/local CLI 兼容入口，不直接作为
默认 runtime 的新插件协议；迁移完成前不删除。

### 4.2 “显式配置”含义

配置插件只能从 operator 提供的 module 名称列表加载，例如：

```text
MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES=my_company.email_plugin,my_company.crm_plugin
```

module 名必须经过保守格式校验和去重。空配置不导入任何外部 module。插件 module 被导入即代表执行
进程内 Python 代码，因此该能力只面向受信任部署，不宣传为不可信沙箱。

### 4.3 默认启用与本轮暴露分离

插件被加载只表示它有资格构造 Tool；Tool 被注册只表示它进入进程级 inventory；Tool 是否进入某轮
`RunToolCatalog.available_tool_names` 仍由 `ToolSpec`、entry profile、media/env 和结构化显式 opt-in
决定：

```text
plugin configured
    -> plugin loaded and validated
    -> tools built and registered
    -> run-scoped qualification/exposure
    -> ActionValidator
    -> ToolExecutor
```

第三方插件声明的 `category`、`enabled_by_default` 等字段不能提升宿主授予的权限。未知或缺失安全声明
继续采用 `dangerous + requires_confirmation` 的保守默认；宿主配置决定插件是否允许加载，run catalog
和 executor 决定 Tool 是否允许暴露与执行。

## 5. 目标架构

```text
ProviderConfig / local operator config
                |
                v
        ToolPluginSources
        /               \
 builtin source     configured-module source
        \               /
                v
          ToolPluginLoader
          - import/shape check
          - api_version check
          - plugin_id/version check
          - duplicate plugin check
                |
                v
          ToolPluginAssembler
          - build_tools(context)
          - validate Tool shape/spec
          - detect duplicate tool names
          - collect issues/provenance
          - no partial commit
                |
                v
          ToolRegistryBuilder
          - register built-in/plugin/MCP tools
          - finalize generation + ownership
                |
                v
       sealed ToolRegistrySnapshot
                |
                v
 AgentGraphRuntime / RunToolCatalog / governed execution
```

实现不必机械按图创建大量类，但必须保持“发现、构造、校验、提交、运行”五个阶段可区分，错误能够
指出 `source -> plugin -> tool`，且失败批次不会留下半注册状态。

## 6. 核心契约

### 6.1 插件描述符

新增轻量 Pydantic 契约，建议放在 `tools/plugins/contracts.py`：

```python
class ToolPluginDescriptor(BaseModel):
    plugin_id: str
    plugin_version: str
    api_version: Literal["tool_plugin_v1"] = "tool_plugin_v1"
```

约束：

- `plugin_id` 使用稳定的小写字母、数字、点、下划线命名，不能为空；
- `plugin_version` 是诊断和兼容标识，不在 L2 内实现版本解析或依赖求解；
- `source_type`、module 名和 trust level 由 loader/host 记录，不能由插件自报；
- descriptor 不复制 Tool name、Schema、category 或 exposure；这些仍由 Tool/ToolSpec 提供。

`ToolPlugin` 调整为：

```python
class ToolPlugin(Protocol):
    descriptor: ToolPluginDescriptor

    def build_tools(self, context: ToolPluginContext) -> list[Tool]: ...
```

迁移期可以为现有只有 `plugin_id` 的内置插件提供一次性兼容适配，但完成标准要求所有内置插件显式
声明 descriptor，避免永久保留两套协议。

### 6.2 配置 module 导出协议

配置 module 必须暴露一个明确入口：

```python
__assistant_tool_plugin__: ToolPlugin
```

首版只接受单 module、单 plugin object，不自动枚举 module globals，不调用任意命名 factory，也不接受
`__assistant_tools__` 混用。一个分发包需要多个插件时配置多个 module，保证错误和 ownership 清晰。

### 6.3 装配记录

为诊断定义结构化记录：

```text
ToolPluginSourceRecord
    source_type
    source_ref
    trusted

ToolPluginLoadIssue
    code
    message
    source_ref
    plugin_id?
    tool_name?

ToolRegistrationRecord
    tool_name
    plugin_id
    plugin_version
    source_type
```

错误信息必须经过现有 secret sanitizer；不得记录环境变量值、token、provider 原始响应或绝对敏感路径。

### 6.4 Registry ownership 和 seal

Registry 对外继续提供现有 `get/list/get_spec/list_specs/run`，新增只读诊断能力：

```text
generation
registration_record(tool_name)
list_registration_records()
sealed
```

装配阶段使用 builder 或未 seal 的 Registry；finalize 后：

- 再调用 `register()` 返回明确错误；
- Tool map 和 registration records 不再原地改变；
- generation 根据排序后的安全契约摘要生成，不包含 secret 或对象地址；
- `RunToolCatalog` 在 L2 不必持久化 generation，但 trace/tool catalog summary 应记录 generation，便于为
  L3 验证积累事实。

不得直接给现有共享 Registry 增加 `unregister()`；这会制造一个没有 run snapshot 保护的伪 L3。

## 7. 配置设计

新增独立的 Tool plugin 配置解析，不把 module 列表混入 Provider readiness：

```text
MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES
```

规则：

- 缺省为空；
- 逗号分隔、去除空白、保持首次出现顺序并去重；
- 非法 module 名在启动装配报告中形成明确错误；
- 配置 module import/build/validation 失败时默认 fail closed，阻止默认 runtime 静默缺失显式要求的
  插件；
- 未配置的可选内置 Provider capability 仍按现有规则跳过，不把“未 ready”误报成插件加载失败；
- mock/real 全局边界保持不变，配置插件的 Provider-backed Tool 同样必须读取结构化 provider mode，不能
  因本机存在 key 自动切换 real；
- 不在日志或报告中输出完整环境配置。

配置对象可以先独立于庞大的 `ProviderConfig`，由 composition root 同时读取；只有确认是稳定公共配置
后再决定是否并入 `ProviderConfig`。避免为了一个 module 列表扩大 Provider 配置职责。

## 8. `ToolPluginContext` 约束

L2 不将 `ToolPluginContext` 改造成无限增长的全局 Service Locator。

- `config` 继续承载现有 Provider/产品结构化配置；
- MCP runner、video stores、durable service 等当前内置插件依赖暂时保留；
- 新配置插件默认只依赖公开 context，并自行创建领域 adapter；
- 新增宿主管理的共享依赖前必须证明其生命周期跨插件共享，不能仅为单个插件方便而加入 context；
- 插件不得获得 `ToolRegistry`、`ActionValidator` 或 `ToolExecutor`，从结构上防止注册和执行治理绕行；
- 若后续依赖持续增长，另立设计将公共 host services 与 built-in-only dependencies 分开，不在本阶段引入
  无类型字典式 locator。

## 9. 分阶段实施

### 阶段 A：固化插件协议和装配报告

1. 在 `tools/plugins/contracts.py` 增加 descriptor、source、issue 和 registration record 契约。
2. 将现有内置插件的 `plugin_id` 迁移为 `ToolPluginDescriptor`，保持 Tool 集合和顺序不变。
3. 新增统一插件 shape 校验：descriptor、`build_tools()`、返回类型和 Tool 基本契约。
4. 为插件构造异常生成脱敏、结构化 issue；不捕获后继续产生部分 Tool 集合。
5. 保持 `ToolSpec` 是 Tool name、Schema 和安全字段的唯一事实源。

验收：现有内置插件构造结果不变；错误能定位到 plugin/source；没有新增中心 Tool manifest。

### 阶段 B：实现显式 module discovery

1. 新增配置 module 名解析和校验。
2. 新增 `ConfiguredModuleToolPluginSource`，只导入显式 module，并读取
   `__assistant_tool_plugin__`。
3. 将当前默认插件元组包装为 `BuiltinToolPluginSource`；保持显式顺序和可信清单，不扫描目录。
4. 合并来源后检查重复 `plugin_id`；配置插件不得覆盖内置插件。
5. 明确 fail-closed：显式配置 module 的 import、协议、API version 或构造失败会使 runtime 初始化失败，
   同时提供安全诊断报告。

验收：测试插件 module 仅通过配置即可进入装配，不修改 `defaults.py`；未配置时不会被导入。

### 阶段 C：原子 Tool 装配和 ownership

1. 将 `build_default_tools()` 演进为返回结构化 assembly result，而不是裸 `list[Tool]`。
2. 在提交 Registry 前完成全部 Tool shape、ToolSpec 和重名校验。
3. 重名策略统一为失败，不允许后加载插件覆盖先加载插件。
4. 将 plugin descriptor/source 写入 Tool registration record，不写入 Provider-visible schema。
5. MCP proxy Tool 在同一最终装配阶段加入 ownership，使用稳定的 source/plugin identity；继续执行 MCP
   server allowlist，不改变 MCP 的发现与执行协议。
6. 保留 `ToolRegistry.register(tool)` 作为测试和显式小型 Registry 的兼容入口，但默认产品装配必须走
   批量校验后提交路径。

验收：任一插件出现重复 Tool 或无效 Tool 时，默认 runtime 不获得半成品 Registry；诊断可回答每个
Tool 来自哪个 source/plugin/version。

### 阶段 D：默认 Runtime 接线与 Registry seal

1. `create_default_registry()` 读取 plugin config、调用统一 loader/assembler 并完成 Registry seal。
2. `AgentGraphRuntime`、`ToolExecutor`、workflow 和 MCP server 默认入口继续复用同一 Registry 行为。
3. 处理 durable task 的循环依赖：在 composition root 内完成 Registry builder、
   `DurableTaskService` 和 `TaskPlanSubmitTool` 的两阶段组装，在 seal 后禁止 runtime 补注册；不得用
   “seal 后临时放行”保留隐式可变性。
4. 专用 realtime video observer Registry 同样经明确 builder/finalize 创建，但只装配其专用视觉 Tool，
   不加载默认配置插件。
5. tool catalog/trace 安全摘要记录 Registry generation；provider schema 和模型 prompt 不包含 plugin
   内部信息。

验收：默认 Runtime 构造结束后 Registry sealed；所有现有入口仍走 mock/offline 主链路；durable 和专用
observer 不在运行期间修改 Registry。

### 阶段 E：收敛普通 Tool 的中心名称特判

本阶段只处理会阻碍“普通配置插件零核心修改”的通用问题，不追求清除全部产品特例。

1. 审计 `registry.py` 中模型不可见输入字段处理，将可通用表达的运行时字段投影移到受控 Tool 契约或
   runtime input binding adapter；身份字段仍由宿主绑定，插件不能自行提供可信值。
2. 审计 `tool_executor.py` 中 memory/vision 名称集合，区分：
   - 通用可信 `user_id/session_id` 注入；
   - request-scoped media 注入；
   - 真正只属于某个内置领域的特殊处理。
3. 普通插件输出继续依赖通用 `summary/message/model_observation`；专用 UI/产品 observation formatter
   允许保留显式公共集成，不把任意插件 formatter 当成可信代码数据通道。
4. 将 `_CODE_CONFIGURED_WRITE_TOOL_NAMES` 的默认暴露事实迁到结构化宿主配置或内置插件 policy，避免
   新普通 write Tool 必须修改中心名称集合；第三方 write/dangerous Tool 默认不暴露，必须显式 opt-in。

验收：新增一个普通 read Tool 和一个默认关闭的 write Tool，只需插件 module、配置和必要测试即可
完成注册及受治理调用；不修改 Registry、Executor 或中心 Tool name 表。

### 阶段 F：文档、CLI 与兼容清理

1. 为插件配置增加只读诊断命令，输出加载状态、plugin id/version、Tool 名和脱敏 issue，不执行 Tool。
2. 更新 `docs/tool-calling-architecture.md`，将 L2 启动装配设为当前权威；开发计划保留实施记录。
3. 更新 README/脚本导航中的配置说明，但不把不可信 Python 插件描述成安全沙箱。
4. 明确 `tools.loader.__assistant_tools__` 的兼容用途；若无默认 runtime 调用方，不将其自动合并到新协议。
5. 使用 `rg` 确认普通插件接入不依赖新的中心 Tool 枚举。

## 10. 测试决策

主要决策：`ADD + EXTEND`。

理由：L2 新增了稳定的插件 module 配置协议、启动失败语义、跨插件冲突处理和 Registry sealed 行为；
这些是外部可观察契约和关键启动边界，现有测试无法充分证明。

### 10.1 `critical` 最小安全网

扩展现有 Tool governance/启动测试，保护：

- 无插件配置时默认 mock Registry 的 Tool inventory 与现状一致；
- Registry finalize 后不可注册新 Tool；
- Tool ownership 和 generation 稳定且不进入 provider-visible Tool schema；
- 显式配置错误默认 fail closed，不静默忽略 operator 要求；
- `RunToolCatalog` 与 `ActionValidator` 仍拒绝未暴露 Tool。

### 10.2 `feature` 插件功能测试

新增聚焦的 L2 装配测试，使用仓库内测试 module/fake plugin 验证：

- 未配置 module 不会 import；
- 配置 module 可以贡献普通 read Tool；
- 无效 module、缺失导出、API version 不兼容形成稳定错误；
- 重复 plugin id 和重复 Tool name 在提交前失败；
- 一个插件构造失败不会留下部分注册；
- 配置插件 Tool 能经过 mock `AgentGraphRuntime` 的 native tool-call 治理闭环；
- write/dangerous Tool 未显式启用时不进入本轮 catalog。

### 10.3 `tools_plugin` 真实配置装配

保留现有显式 opt-in 真实 Provider/MCP 插件测试。若配置插件需要真实 Provider，只验证构造和 readiness，
不发起付费调用。默认 pytest 不读取或导入 operator 的外部插件配置，测试应隔离相关环境变量。

建议验证命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q <L2 定向测试>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/feature/test_tool_plugin_runtime.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
git diff --check
```

默认验证只走 mock/local/offline，不调用真实 Provider，不安装新依赖。

## 11. 迁移与提交策略

按可独立验证、可回滚的阶段提交：

1. `docs: plan startup-pluggable tool plugins`
2. `refactor: add versioned tool plugin descriptors`
3. `feat: load explicitly configured tool plugin modules`
4. `refactor: assemble tool plugins atomically with ownership`
5. `refactor: seal runtime tool registries after startup`
6. `refactor: remove generic tool-name integration branches`
7. `docs: document startup-pluggable tool registration`

兼容策略：

- 内置默认 Tool 名称、ToolSpec 和 mock 行为保持不变；
- 现有本地 `__assistant_tools__` loader 暂不删除或自动迁移；
- 配置插件协议从 `tool_plugin_v1` 开始，不承诺接受未版本化对象；
- 不允许配置插件覆盖同名内置 Tool；需要替换内置能力时另立 Provider/adapter 设计，而不是借插件加载
  顺序实现隐式 monkey patch；
- durable task 在迁移前已有的 Tool name 继续按稳定名称解析，本阶段不引入 plugin version pinning。

## 12. 风险与控制

### 12.1 进程内代码执行风险

导入 Python module 本身即可执行任意进程权限代码。控制措施是只加载 operator 显式配置的 module、
不扫描、不从用户请求读取 module 名、不把配置插件描述为沙箱。需要不可信隔离的能力应使用 MCP 或
独立服务。

### 12.2 “插件声明即授权”风险

ToolSpec 是契约但不是授权来源。第三方插件的 category/默认暴露声明必须经过宿主 policy；write 和
dangerous 默认 fail closed，确认、身份和执行校验继续由宿主完成。

### 12.3 部分注册和顺序依赖

逐插件直接写 Registry 会使后续失败留下半成品。必须先构造临时 assembly、完成全量冲突校验，再一次
性 finalize。插件不得依赖另一个插件已经写入 Registry；真正共享依赖由 composition root 显式提供。

### 12.4 Context 膨胀

每个插件都要求增加一个 context 字段会重建中心耦合。领域 client 优先由插件通过结构化配置创建；只有
宿主管理且确实跨插件共享的服务才进入公共 context。

### 12.5 Registry seal 与现有晚注册

durable task 当前存在 Registry 创建后补注册路径，是本阶段最大接线风险。seal 上线前必须将其收敛到
启动 composition root；不能为了兼容而长期保留运行期可变后门。

### 12.6 过早设计 L3

L2 只记录 generation/ownership，不实现 unload、drain 或旧 snapshot 执行。若本阶段引入半成品
`unregister()`，会让已发给 LLM 的 schema、durable plan 和真实 Registry 不一致，应明确拒绝。

## 13. 完成标准

- 新的普通配置插件只需提供 `__assistant_tool_plugin__` 并加入显式 module 配置，重启后即可进入默认
  Runtime，无需修改核心默认插件清单。
- 未配置的外部 module 不会被 import；不存在目录扫描或隐式 entry point 加载。
- 所有内置插件使用 `tool_plugin_v1` descriptor，并经过同一装配校验。
- Registry 可以报告 generation 及 `tool -> plugin/source/version` ownership。
- 默认 Runtime 使用原子装配后的 sealed Registry，运行期间不再补注册 Tool。
- 插件或 Tool 冲突、协议不兼容和显式配置构造失败均 fail closed，且输出脱敏诊断。
- mock/real、MCP allowlist、RunToolCatalog、ActionValidator、ToolExecutor 和 confirmation 边界不回退。
- 普通 read Tool 与默认关闭 write Tool 的测试插件可以不修改中心 Tool 名表完成治理闭环。
- 权威文档、README/配置导航和测试说明与实现一致。
- 定向测试、默认离线 pytest 和 `git diff --check` 通过。

## 14. L3 后续入口

L2 完成后，只有出现明确的“不重启启停插件”产品需求，才单独设计 L3。L3 至少需要：

- immutable Registry snapshot 与原子 generation swap；
- 每个 run 固定 registry generation；
- 插件 `start/health/drain/stop` 生命周期；
- 活动 tool call 引用计数和排空；
- durable plan 的 tool/plugin version pinning 或迁移策略；
- 更新失败回滚、旧 snapshot 回收和可观测状态机；
- 管理入口的身份、授权和审计。

L2 的 descriptor、ownership 和 generation 是这些能力的准备数据，但不得被当作已经支持热插拔。

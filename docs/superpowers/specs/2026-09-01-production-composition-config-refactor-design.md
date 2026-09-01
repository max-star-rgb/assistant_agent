# 生产装配与配置重构设计

日期：2026-09-01  
状态：设计已确认，实施计划已完成

## 1. 文档定位

本文是“项目物理架构重构总纲”的第一切面设计，范围限定为生产启动、配置装载和依赖装配。
它属于开发设计材料，不替代当前 authority；运行时事实仍以 `AGENTS.md`、`docs/authority.toml`
路由到的专项文档、源码和测试为准。

本切面只重塑内部配置 API 和依赖传递，不改变产品功能、环境变量契约、Provider 安全边界或生产 Graph 身份。

## 2. 当前问题

`src/assistant_agent/config/__init__.py` 当前约 1,431 行，其中 `ProviderConfig` 有 188 个字段，实际覆盖：

- Provider 模式与 Chat；
- Vision、Embedding、实时视觉和视觉记忆；
- 长期 Memory 与对话历史；
- 上下文压缩和 Agent Server 超时；
- 图片生成、搜索、购物、住宿；
- 媒体、主动投递、3D 和 durable task。

因此 `ProviderConfig` 的名称与职责不符，并且同时承担配置模型、环境变量读取、兼容回退、默认值和校验。
大量下游模块接收完整配置对象，使真实依赖无法从函数签名判断。

生产装配本身已有正确中心：`AgentServerExecutionOwner.compose()`。问题不是缺少新的 bootstrap 层，而是完整配置
没有停留在该 composition root，下游也没有按消费边界接收配置。

## 3. 已确认约束

- 允许调整仓库内部 Python 配置 API。
- 保留全部现有环境变量、默认值、清洗规则、兼容回退和校验行为。
- 保留 `mock/real` 的显式安全边界，不允许真实 Provider 静默回退到 mock。
- 本切面不删除旧配置能力或历史环境变量；无用项识别和删除留给后续切面。
- 不引入长期 `ProviderConfig` alias、代理属性或双配置路径。
- 不移动 Tool、Media、Memory、Runtime 等领域实现；只调整其配置签名和依赖传递。
- 不调用真实 Provider。

## 4. 采用方案

采用“集中加载、按真实消费者分组”的方案：

```text
环境变量
   ↓
config.env.load_app_config()
   ↓
AppConfig
   ↓
AgentServerExecutionOwner.compose()
   ├─ Chat 配置或 ResolvedProviderSpec → Chat model
   ├─ Vision 配置或 ResolvedProviderSpec → 视觉资源
   ├─ MemoryConfig → Memory backend
   ├─ MediaConfig → 媒体资源
   ├─ ToolConfig → Tool inventory
   └─ RuntimeConfig → native graph
```

没有采用以下方案：

- 仅拆文件并保留扁平 `ProviderConfig`：只能改善文件长度，不能消除依赖扩散。
- 配置完全下沉到每个业务包：会分散环境解析和兼容回退，并增加循环依赖风险。

## 5. 所有权与依赖方向

### 5.1 `config`

只负责：

- 配置 dataclass；
- 环境变量读取、清洗、类型转换和兼容回退；
- 配置段内部及跨段的纯数据校验。

不负责：

- 创建 Provider client、Tool、Memory backend 或 Graph；
- 根据自然语言选择能力；
- 管理运行时资源生命周期。

### 5.2 `agent_server/services.py`

`AgentServerExecutionOwner.compose()` 继续作为唯一持有完整配置并向业务下游传递它的 production composition root：

- 唯一持有完整 `AppConfig`；
- 创建进程级模型、Tool inventory、Memory backend、MCP pool 和线程资源；
- 把窄配置段或已解析依赖传给下游；
- 编译并持有三个生产 Graph。

不增加新的 `bootstrap`、`container` 或单实现 factory 层。

### 5.3 `agent_server/graph.py`

继续保持 LangGraph 薄入口，只负责取得进程级 owner 并暴露 Graph，不读取业务环境变量或构造领域对象。

### 5.4 `native_agent/assistant_agent.py`

继续负责 native Assistant、worker 和 middleware 组合，但只接收已经解析的运行参数和依赖；不得读取环境变量，
也不得持有完整 `AppConfig`。

### 5.5 `providers` 与其他领域

`providers/specs.py` 继续拥有 Provider 描述、选择和解析结果。下游优先接收 `ResolvedProviderSpec` 或自身配置段，
不反向依赖完整应用配置。

`agent_server/media_app.py` 的 FastAPI lifespan 还负责视觉模块、远程视频归档和主动投递的进程资源。为保持现有
启动时序，它可以在入口边界调用 `load_app_config()`，但必须立即投影为 `vision`/`media` 配置；不得保存完整对象，
也不得向叶子模块传递完整对象。本切面不为消除这一次入口读取而新增全局配置容器。

现有 `mcp/config.py` 与 `observability/langsmith_config.py` 已有独立、清楚的 owner，继续由原模块加载，
不并入万能配置中心。

## 6. 配置模型

第一版采用最少的真实消费分组：

```text
AppConfig
├── provider_mode
├── runtime
├── chat
├── vision
├── memory
├── media
└── tools
    ├── image_generation
    ├── search
    ├── shopping
    └── lodging
```

具体规则：

- 不为每个 Provider 创建一套配置类。
- 只有需要被单独传给消费者的配置才形成独立 dataclass。
- 共用 API key 的兼容回退在 `env.py` 中一次完成，各配置段获得最终有效值。
- 配置段自身的约束放在对应 dataclass；真正跨段的约束由 `AppConfig` 校验。
- 不建立 `config/sections/**` 目录树。

初始物理结构为：

```text
config/
├── __init__.py   # 稳定、很薄的包级导出
├── models.py     # AppConfig 和配置段 dataclass
└── env.py        # 唯一核心环境变量装载入口
```

只有跨段校验多到使 `models.py` 职责再次失衡时，才增加 `validation.py`；本切面不预建空结构。

## 7. 兼容边界

本切面兼容的是外部运行契约，而不是未承诺的仓库内部类结构：

- 环境变量名称不变；
- 缺省值不变；
- 空白清洗、布尔值和数值解析不变；
- `DASHSCOPE_API_KEY` 等既有 alias/fallback 不变；
- 相同环境输入产生相同有效配置；
- 非法输入继续在装配阶段失败；
- 异常类型和主要错误信息保持一致。

迁移完成后删除 `ProviderConfig`。仓库内调用方一次性更新，不保留 re-export、扁平属性代理或新旧模型同步逻辑。

## 8. 迁移顺序

1. 在旧 `ProviderConfig` 仍存在时加入嵌套模型和新环境加载器。
2. 建立新旧配置等价性检查，覆盖全部默认字段及高风险环境组合。
3. 更新 `AgentServerExecutionOwner.compose()` 使用 `AppConfig`。
4. 更新下游消费者，只传递其配置段或已解析依赖。
5. 删除 `ProviderConfig` 和旧加载路径。
6. 更新受影响的当前 authority 和入口说明；不批量改写历史文档。

这六步属于同一切面，中间并存只用于迁移和验证，不形成长期双轨。

## 9. 等价性检查

在删除旧模型前，把新配置展开为旧字段语义并逐项比较。至少覆盖：

- 空环境下的全部默认值；
- Chat、Vision、Embedding、Image Generation 的显式 Provider 配置；
- Qwen/DashScope、Ark 等既有 key 回退；
- `mock` 与 `real` 模式；
- Memory、视觉阈值、上下文压缩比例和 timeout 的合法边界；
- 缺少必要真实 Provider 配置及非法数值的失败行为。

等价性辅助只服务迁移，不进入生产 Runtime。完成后保留最小必要契约测试，不把旧扁平结构固化为永久公共 API。

## 10. 验收标准

- `config/__init__.py` 仅保留稳定导出。
- 生产源码不再引用 `ProviderConfig`。
- 完整 `AppConfig` 只由 composition root 持有和向下传递；`media_app` 入口加载后立即投影，不保存完整对象。
- `native_agent`、Provider factory、Tool plugin、Memory 和 Media 消费者不读取环境变量。
- 相同环境输入得到相同有效配置和失败行为。
- `mock` 模式不初始化或调用真实 Provider。
- `real` 模式缺少配置时不静默回退。
- `assistant-native-v4`、`assistant-worker-v2`、`assistant-memory-v1` 的身份和入口不变。
- `langgraph.json` 与 `agent_server/graph.py` 继续保持薄入口。
- 受影响定向测试、`tests/core` 和文档 authority validator 通过。
- 现有 8089 开发服务完成 hot reload，并能加载三个生产 Graph。

## 11. 非目标

- 不删除或重命名环境变量。
- 不重新设计 Provider 选择规则。
- 不改变 Tool exposure、HITL、Memory lifecycle 或视觉流水线。
- 不重写 middleware。
- 不移动或清理 `runtime`、`context`、`tools`、`media` 等目录。
- 不重组 MCP 或 LangSmith 配置。
- 不批量整理历史设计文档。

上述内容分别留给后续切面或独立功能任务。

实施步骤见
[`2026-09-01-production-composition-config-refactor.md`](../plans/2026-09-01-production-composition-config-refactor.md)。

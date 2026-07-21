# Tool Manifest 删除重构方案

日期：2026-07-21

## 1. 背景

内置 Tool 已按能力域迁入 `tools/plugins/<capability>/`，Tool 的 `name`、`description`、输入输出
Schema、`category`、`toolset`、确认与媒体要求也由 Tool 类自身声明。现有
`services/tool_manifest.py` 仍重复维护 public name、exposure class、capability、planner action、legacy
alias 和 provider binding，导致新增或删除 Tool 仍可能需要修改中心表，不符合插件自包含目标。

本次重构删除 `services/tool_manifest.py`，但不改变 Tool 名称、Provider 选择、Runtime 工具目录、旧
action/intent 兼容结果或 ToolResult capability 输出。

## 2. 目标

- 删除 `ToolManifest`、`TOOL_MANIFESTS` 及其派生索引。
- Tool 注册、Spec 和 exposure 继续只以 Tool 类及 `ToolRegistry` 为事实源。
- 新增普通插件 Tool 不需要登记任何中心 manifest。
- 将仍被跨层共享的稳定字符串缩减为轻量协议常量，不携带注册、分类或暴露语义。
- 将旧 intent/action/capability 映射明确隔离为 legacy compatibility，而不是 Tool catalog。
- capability 结果继续由具体 Tool/内部执行分支产生，尤其保留
  `vision_understanding -> image_understanding | video_understanding` 的区别。

## 3. 非目标

- 不重写 assistant loop、planner 或 intent router。
- 不改变现有公开 Tool 名称。
- 不移除仍被现有入口消费的 legacy action/intent alias。
- 不引入目录扫描、动态插件发现或新的全局 Tool 注册表。

## 4. 目标所有权

| 现有职责 | 目标位置 | 说明 |
| --- | --- | --- |
| 跨层稳定 Tool/capability 字符串 | `schemas/tool_ids.py` | 仅保存确实被插件外代码共享的协议标识；不是注册表，新 Tool 默认无需加入。 |
| Capability 类型、契约与 alias 数据入口 | `schemas/capabilities.py` | capability 是结果/协议词汇，不决定 Tool 注册与暴露。 |
| capability、planner action、legacy alias 映射 | `agent/legacy_tool_mapping.py` | 仅服务旧 planner/intent 兼容调用方。 |
| Tool 名称、category 和 schema | 各 `tools/plugins/<capability>/` Tool 类 | Registry 生成 ToolSpec 的唯一来源。 |
| Provider readiness | 各插件 `plugin.py` | 决定 real/mock 模式下是否构造真实 Tool。 |
| provider bindings | 删除 | 当前没有外部调用方，不保留死接口。 |
| exposure class | 删除 | 已由 `ToolSpec.category` 表达。 |

`schemas/tool_ids.py` 只解决现有跨层协议代码不能反向导入 Tool 实现的问题。它不枚举 Tool、不提供
查询 API，也不要求新增 Tool 同步；只有某个 Tool 名称成为跨层稳定协议时才增加常量。

## 5. 实施步骤

1. 在 `schemas/tool_ids.py` 放置现有跨层共享 Tool/capability 协议常量。
2. 在 `schemas/capabilities.py` 保留 capability 类型、契约以及 legacy intent alias 的公开入口。
3. 新建 `agent/legacy_tool_mapping.py`，迁移仍在使用的 canonical 映射函数。
4. 将 Tool、Provider、Memory、MCP、Runtime、Gateway 与测试的常量导入迁离 manifest。
5. 将 router、intent、executor、tool input builder 的映射导入迁到 legacy 模块。
6. 删除未使用的 manifest 查询、provider binding 和 exposure-class 数据。
7. 删除 `services/tool_manifest.py`，使用 `rg` 确认没有残留导入或文本引用。
8. 更新 `docs/tool-calling-architecture.md`，明确不存在中心 Tool manifest。

## 6. 测试决策

决策：`EXTEND`。

这是行为保持的架构重构，但 legacy action/capability 映射和 ToolResult capability 会跨越 planner、
executor 与插件边界。扩展现有 `tests/critical/test_tool_governance.py` 中相关契约，证明：

- Registry 仍从 Tool 类生成同名 ToolSpec；
- 关键 legacy action/capability 映射保持不变；
- `vision_understanding` 的 image/video capability 区分保持不变。

验证命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/critical/test_tool_governance.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
```

默认验证只使用 mock/local/offline，不调用真实 Provider。

## 7. 完成标准

- `src/assistant_agent/services/tool_manifest.py` 不存在。
- `src/`、`tests/`、`scripts/` 不再导入 `assistant_agent.services.tool_manifest`。
- 默认 Registry Tool 名称和插件测试保持通过。
- legacy 映射结果及 runtime tool-call 闭环保持通过。
- 新增普通 Tool 的常规修改面仍是对应插件目录和对应插件测试。

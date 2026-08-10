# Skill 驱动的渐进式工具暴露设计

## 讨论结论

本次 Trace 暴露的核心问题不是模型不会选择工具，而是 Runtime 在第一次模型调用前已经默认激活旅行
Skill，并把大量领域 Tool schema 同时暴露给模型。普通研究请求因此可能被 Gmail、地图、住宿等工具
吸走。

已确认的方向如下：

1. 保留“主模型直觉式回答”的一次调用快速路径，不增加独立意图模型；Qwen Provider-native 联网仍是
   模型生成能力，不投影为本地 Tool。
2. Skill 从“默认提示词附件”改为真正的能力激活入口：第一次只展示 Skill card 和很小的控制工具；
   `load_skill` 成功后，下一次模型调用才获得该 Skill 的正文和领域 Tool schema。
3. `SKILL.md` 只保存程序性指导；`skill.toml` 保存机器可读的激活方式、版本、可发现性、Tool grants
   和 reference 注册表。
4. Skill 激活产生统一的 `CapabilityGrant`。Grant 在当前 run 立即生效，并在同一 user/agent/session 内持续
   保存；第一版没有 TTL、清除、话题切换检测或 `SessionCapabilityLease`。
5. 多模态不另造一套 exposure mode。所有 Skill 使用同一激活模型：
   - `activation = "model"`：由模型调用 `load_skill`，适用于旅行、邮箱日历、视觉创作；
   - `activation = "context"`：由已有结构化 entry/media/env 资格自动激活，适用于图片、视频、实时
     画面和视觉记忆。`skill.toml` 不复制媒体判定规则。
6. 当前不实现自定义 `tool_search`。百炼公开 API 没有原生 deferred tool search，而只有两三个试验工具
   时自研搜索/排序收益不足。`CapabilityGrant.source` 预留 `tool_search`，未来可由本地检索或
   Provider-native adapter 产生同一 Grant。

## 范围

本次实现：

- `skill.toml` loader 与纯程序性 `SKILL.md`；
- `CapabilityGrant`、当前 run 动态扩展、Session 持久化和恢复；
- `model` / `context` 两种 Skill 激活来源；
- 旅行、邮箱日历、视觉创作、结构化视觉输入四组 Skill；
- Tool catalog、context、runtime、session store 和可观测字段同步。

本次不实现：

- `tool_search` 控制工具、语义检索、embedding 或 namespace 排序；
- Skill 清除、TTL、租约或自动失效；
- 基于用户文本的关键词、正则或手写路由；
- Tool Provider 错误分类、同轮 batch 熔断或 durable research workflow。

## 统一数据模型

```text
ToolRegistry（启动期注册并 seal）
  -> 结构化 entry/media/env 上限资格
  -> eligible ToolSpec

SkillCatalog
  ├─ skill.toml：机器契约
  └─ SKILL.md：程序性正文

CapabilityGrant
  ├─ source: skill | context | tool_search（预留）
  ├─ grant_id
  ├─ skill_id
  └─ tool_names

SessionRecord.capability_grants
  -> 下个 turn 恢复到 AgentState.capability_grants

eligible ToolSpec
  ∩（baseline/unclaimed tools ∪ active CapabilityGrant.tool_names）
  -> RunToolCatalog
  -> ChatRequest.tools
```

`ToolRegistry` 始终拥有实现、schema、category、repeat policy 与执行治理。Skill 只授予“哪些已注册工具
可以进入模型可见目录”，不能注册 Tool、复制 Tool schema，也不能绕过 entry allowlist、媒体要求、
Provider readiness、Validator 或 Executor。

## Skill 文件契约

目录：

```text
skills/<skill_id>/
├── skill.toml
├── SKILL.md
└── references/（可选）
```

第一版 manifest：

```toml
schema_version = 1
skill_id = "travel-tool-orchestration"
version = 3
description = "用于住宿、通勤和旅行行程决策。"
enabled = true
discoverable = true
disable_model_invocation = false
activation = "model"
governed_tools = ["lodging_search", "mcp.amap_maps.maps_text_search"]

[references]
# guide = "references/guide.md"
```

约束：

- `skill_id` 与目录名一致；`schema_version` 当前只能为 `1`；
- `governed_tools` 非空且无重复；
- `activation` 只能为 `model` 或 `context`；
- `context` Skill 不进入第一轮 Skill index，而是在至少一个 governed Tool 已通过结构化资格检查时激活；
- reference 只能是一层 `references/*.md` 注册路径，读取时继续做真实路径与 symlink 防护；
- `SKILL.md` 不含 frontmatter、受治理工具、权限或可见性机器章节；正文可以提及 Tool 名称和组合流程，
  但不能复制输入 schema 或返回 schema。

## Catalog 与激活

### 第一次模型调用

1. 先基于 Registry、entry allowlist、media/env 和 Provider readiness 计算 `eligible ToolSpec`；
2. 自动产生符合结构化事实的 `context` Skill grants；
3. 暴露控制工具、未被任何启用 Skill claim 的兼容工具，以及 active grants 覆盖的 eligible tools；
4. 把尚未激活、可发现且存在 eligible governed tool 的 `model` Skill 作为 `<skill_index>` card；
5. 不把 Skill card 当成 active Skill，不加载其正文，也不暴露其领域工具。

受信任 durable/workflow 的结构化 allowed-tools 仍直接决定其工作项目录，不强制先加载用户领域 Skill。
调用方 `metadata.tool_visibility.enabled_skills` 不再能激活 Skill。

### `load_skill` 成功后

`load_skill` 仍完整经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。成功结果交给 Runtime
控制处理器；处理器不信任 observation 中的 Tool 名单，而是按返回的 `skill_id` 重新读取可信
`SkillCatalog`：

1. 校验该 Skill 已启用、为 `model` 激活且允许模型调用；
2. 构造或替换 `CapabilityGrant`；
3. 立即写入 `AgentState.capability_grants`；
4. owner-scoped、幂等写入 `SessionStore`；
5. 下一次 context build 从 Registry 重新投影 ToolSpec，并加载可信 `SKILL.md` 正文。

同一个 Provider tool-call batch 中，在 `load_skill` 之前未暴露的领域调用仍会被 Validator 拒绝；动态
扩展只对后续模型调用生效。

### Session 恢复

新 run 创建后，Runtime 按 user/session 读取持久化 Grant：

- Skill 已删除、禁用或激活类型不匹配时忽略旧 Grant；
- Tool 名单从当前 manifest 重建，不信任历史序列化名单；
- 当前 turn 仍重新应用 entry/media/env 上限，所以持久化 Grant 不能恢复本轮不合格 Tool；
- Session 删除时 Grant 随 `SessionRecord` 删除；本阶段没有单独 deactivate API。

## Skill 分组

- `travel-tool-orchestration`（model）：住宿和地图旅行工具；
- `workspace-communications`（model）：`email_search`、`email_read`、`calendar_search`、
  `calendar_create`、`contacts_search`；
- `visual-creation`（model）：`image_generation`、`image_to_3d`；
- `visual-context`（context）：`media_inspect`、`live_view_inspect`、`visual_image_search`、
  `visual_memory_search`、`visual_reminder_manage`。

未迁移到 Skill 的 Tool 暂按兼容 baseline 暴露，避免本次重构让不相关能力静默消失。后续迁移只需新增
manifest，不修改 catalog 算法。

## 失败与可观测性

- manifest 缺失、解析失败、ID 不匹配、正文含机器章节或 reference 越界：该 Skill 不进入 catalog，
  记录结构化 loader issue；
- governed Tool 当前未注册或不合格：只是不进入本轮投影，不凭 Skill 恢复；
- `load_skill` 失败：不创建 Grant；
- Session 持久化失败：当前 run Grant 可继续生效，同时记录结构化 Runtime error；
- context report/trace 记录 `discoverable_skill_ids`、`active_skill_ids`、`capability_grant_ids`、
  `session_restored_grant_ids` 和实际 `skill_granted_tool_names`；不复制完整 Skill 正文或 Session 内容。

## 验收标准

- 普通首轮不再默认出现旅行、邮箱日历、视觉创作 Tool schema；
- 首次旅行请求可先 `load_skill`，下一模型调用立即获得旅行正文与 eligible 旅行工具；
- 同 session 后续 turn 无需再次 `load_skill`；
- 图片/视频等结构化输入自动获得对应视觉正文和 eligible 视觉工具，不做文本意图识别；
- 调用方 metadata 不能伪造 Skill 激活；
- Qwen `enable_search=true` 行为不变；
- 全流程不增加独立意图 LLM、关键词路由或自定义 `tool_search`。

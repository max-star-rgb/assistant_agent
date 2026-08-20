# AI Coding Agent 设计规格

日期：2026-08-20

## 1. 目标

在不替换现有 `AssistantRootGraph`、不引入第二套生产 Agent Runtime 的前提下，为
`assistant_agent` 增加分阶段演进的 AI Coding 能力。首阶段交付可实际使用的最小编辑闭环：Agent
在隔离 Git worktree 中检查仓库、生成 patch，经确定性校验和人工审批后原子应用，但不执行测试命令、
不提交 Git、不合并目标分支。

后续阶段依次增加受控验证、受控 commit/merge、强隔离命令执行和高级 coding 工作流。每一阶段必须
独立验收，不能通过未来阶段的安全能力补偿当前阶段的边界缺失。

## 2. 非目标

阶段 1 明确不提供：

- 任意 shell 或进程执行；
- 测试、lint、format、build 或依赖安装；
- 删除、重命名、权限修改和二进制文件写入；
- Git commit、merge、push、PR 或部署；
- 直接修改调用服务的当前开发目录；
- 多 worker 并行写入同一个 workspace；
- 真实 Provider、网络访问或宿主凭据注入；
- 用 prompt、自律指令或模型判断替代确定性授权和隔离。

## 3. 参考实现与选型

参考 LangChain 官方项目与文档：

- [Deep Agents](https://github.com/langchain-ai/deepagents)：参考 pluggable backend、文件 Tool、HITL、
  context offload 和有界 Tool 输出；
- [Deep Agents Sandbox Pattern](https://docs.langchain.com/oss/python/deepagents/frontend/sandbox)：参考
  thread-scoped sandbox/workspace 生命周期，以及文件树、diff、chat 的分层；
- [Open SWE](https://github.com/langchain-ai/open-swe)：参考 coding/review 分图、异步任务、隔离执行和
  服务端持有身份/凭据；
- [Deep Agents Permissions](https://docs.langchain.com/oss/python/deepagents/permissions)：参考路径级
  `allow/deny/interrupt`，但不把它当作 shell 或自定义 Tool 的完整安全边界。

采用“原生主图 + 自定义 CodingGraph + 受信 workspace backend”的组合路线：保留本项目当前
LangGraph/LangChain 原生主链，选择性借鉴 Deep Agents 的 backend 和 Tool 设计，不整体嵌入另一套
opinionated harness。安全边界在 Tool/backend 和确定性 Graph 节点中实现，不依赖模型自我约束。

## 4. 总体架构

生产入口仍只有一个 `AssistantRootGraph`：

```text
AssistantRootGraph
  -> capture_trusted_runtime_facts
  -> memory_recall
  -> execution_router
       fast
       planning
       coding -> AssistantCodingGraph
  -> refresh_memory_extraction
  -> END
```

公开输入的 `execution_mode` 扩展为 `fast|planning|coding`。模式只由结构化输入选择，不从用户文本、
关键词、Memory 或 Tool 调用推断。

阶段 1 的 `AssistantCodingGraph` 是顺序状态机：

```text
resolve_workspace
  -> inspect_and_draft
  -> validate_patch
  -> approval_interrupt
  -> apply_patch
  -> summarize
```

`inspect_and_draft` 复用标准 `create_agent`、`BaseTool`、`ToolNode` 和 messages channel，但使用 coding
专属 Tool inventory 与 prompt。`validate_patch`、`approval_interrupt`、`apply_patch` 是确定性节点，
不允许模型直接跳过或调用。coding 不复用 planning graph 的并行 worker，避免共享 workspace 的写入竞态。

父图仍由 Agent Server 注入 checkpoint、thread、run、cancel、interrupt/resume 和 Store。项目不保存
平行 run 生命周期或自建恢复协议。

## 5. Workspace 服务

新增受信 `CodingWorkspaceService`。它属于 coding backend，不属于模型 Tool 层，以
`user_identity + thread_id + source_repo_id` 作为隔离键。

### 5.1 来源与生命周期

- `source_repo_id` 只能从服务端静态 allowlist 解析，模型不能提供宿主任意路径；
- 每个 coding thread 创建独立临时 Git worktree 和受管临时分支；
- worktree 从任务开始时冻结的 `base_commit` 创建；
- workspace 根目录由服务端分配，模型和客户端都不能提交绝对路径；
- 同一认证用户和 thread 重连时解析到同一 workspace；
- 不同用户或 thread 不得读取、审批或修改彼此 workspace；
- workspace 使用租约和 TTL，活动 thread 可续租，终态后延迟清理；
- 阶段 1 不自动把 worktree 变更写回原仓库。

### 5.2 路径与内容限制

- 所有操作使用仓库相对路径；
- 拒绝绝对路径、空路径、`.`、`..`、隐藏控制目录和 symlink escape；
- 拒绝 `.git/**`、`.env*`、凭据文件、锁文件及配置的 protected globs；
- 只允许读取受限大小的普通文件；
- 只允许修改已存在的 UTF-8 文本文件，以及创建白名单后缀的新 UTF-8 文本文件；
- 禁止删除、重命名、权限修改、设备文件、FIFO、socket、二进制文件和超大文件；
- 限制单文件大小、读取窗口、搜索结果、patch 总字节、目标文件数量和单次 Tool 输出。

### 5.3 一致性与原子性

- proposal 保存 `base_commit` 和所有目标文件的内容摘要；
- validation 和 apply 前均重新读取实际仓库事实；
- `base_commit`、文件摘要或 patch digest 变化时 fail closed；
- patch 先在隔离临时副本执行 dry apply；
- 全部路径和内容通过校验后才原子更新 worktree；
- 任何目标失败都不得保留部分修改；
- 原子恢复失败时冻结 workspace，禁止继续写入并记录高严重度审计事件。

## 6. 模型可见 Tool

阶段 1 暴露以下 coding 专属 Tool：

```text
coding_repo_list(path, depth, cursor)
coding_repo_search(query, paths, globs, cursor)
coding_repo_read(path, start_line, end_line)
coding_repo_status()
coding_repo_diff()
coding_propose_patch(patch, summary)
```

这些 Tool：

- 均由官方 `@tool` factory 创建为标准 `BaseTool`；
- 使用 `ToolRuntime[AssistantRunContext]` 获取运行事实；
- 不向模型暴露 `workspace_root`、`user_id`、`thread_id` 或宿主路径；
- 通过 `server_info.user.identity` 获取唯一受信身份；
- 返回标准 `ToolMessage(content, artifact)`，模型只读取有界投影；
- 读取类 Tool 标记 `effect=read`；
- `coding_propose_patch` 标记 `effect=generate`，只创建候选 proposal，不写文件。

真正的 patch 应用只存在于受信 Graph 节点中，不注册为模型可见 Tool。阶段 1 不注册 coding shell、
Git commit、merge、push 或通用文件写入 Tool。

## 7. State 与数据模型

`CodingState` 在标准 `AgentState.messages` 基础上增加：

```python
class CodingState(AgentState):
    workspace_ref: str | None
    base_commit: str | None
    proposal: CodingPatchProposal | None
    validation: CodingPatchValidation | None
    approval_status: Literal["pending", "approved", "rejected"] | None
    applied_result: CodingPatchApplyResult | None
```

候选 patch 的稳定数据模型：

```python
class CodingPatchProposal(BaseModel):
    patch: str
    summary: str
    changed_paths: tuple[str, ...]
    base_commit: str
    base_file_digests: dict[str, str]
    patch_digest: str
```

模型提交的 `changed_paths`、摘要和其他声明不作为授权事实。validator 必须重新解析 unified diff，独立
计算目标路径、base file digest、patch digest、字节数和操作类型，并输出
`CodingPatchValidation`。只有 validation 成功的 proposal 可以进入审批节点。

`workspace_ref` 是不泄露宿主绝对路径的 opaque ID。Graph state/checkpoint 不保存 workspace 客户端、
文件句柄、Git 进程或原始审计对象。

## 8. HITL 审批

审批采用 LangGraph 原生 interrupt/resume。阶段 1 每份完整 patch 进行一次整批审批，interrupt 只携带
有界、确定的信息：

```json
{
  "action": "coding_patch_apply",
  "workspace_ref": "opaque-reference",
  "base_commit": "commit-sha",
  "patch_digest": "sha256-digest",
  "changed_paths": ["relative/path.py"],
  "summary": "bounded summary",
  "diff_preview": "bounded unified diff"
}
```

恢复只接受 `approve`、`reject` 或 `respond`：

- `approve` 只授权 interrupt 中指定的 patch digest；
- `reject` 清除待应用状态并结束本轮写入；
- `respond` 把用户意见作为新输入返回 drafting，清除旧 proposal，重新生成、验证和审批；
- 阶段 1 不接受恢复 payload 直接编辑 patch，避免绕过 validator；
- 审批后若 workspace、base commit、文件摘要或 patch digest 变化，授权立即失效；
- apply 节点不能重新调用模型，也不能替换或修复已批准 patch。

## 9. 错误与审计

稳定错误码按边界划分：

- Workspace：`workspace_not_allowed`、`workspace_expired`、`workspace_identity_mismatch`；
- 路径：`path_invalid`、`path_protected`、`symlink_escape`、`file_type_unsupported`；
- 基线：`base_commit_changed`、`file_digest_changed`；
- Patch：`patch_invalid`、`patch_too_large`、`patch_path_mismatch`、`patch_apply_conflict`；
- 审批：`approval_required`、`approval_rejected`、`approval_digest_mismatch`；
- 原子性：`patch_apply_failed`、`rollback_failed`。

预期失败返回结构化结果，不暴露宿主绝对路径、完整源码、底层命令或原始 Git stderr。审计只保存认证
主体引用、thread/run、workspace ref、相对路径、摘要、字节数、状态、错误码和时间，不保存完整源码、
完整 patch、Provider 原始响应或可能包含秘密的命令输出。

## 10. 分阶段路线

### 阶段 1：最小编辑闭环

提供仓库 list/search/read/status/diff、候选 patch、确定性 validation、整批 HITL 和原子 apply。变更仅
存在于 thread-scoped worktree。

### 阶段 2：受控验证闭环

增加服务端配置的命令 allowlist：测试、lint、format 和 build。命令由稳定 command ID 映射为固定 argv，
模型不能提交 `bash -c`、管道、重定向、命令替换或任意 cwd。增加超时、CPU、内存、进程、输出和磁盘
限制。format 造成的新 diff 必须重新进入 patch 审批。

### 阶段 3：受控 commit 与合并

在阶段 2 验证通过后创建受控临时 commit，并支持合并到服务端配置的主分支：

```text
final diff
  -> verification evidence
  -> controlled commit
  -> target branch preflight
  -> merge preview
  -> independent HITL
  -> fast-forward or deterministic conflict-free merge
```

merge 审批绑定 `source_commit + expected_target_head + merge_preview_digest`。目标 HEAD 变化、目标工作区
不干净或发生冲突时停止，不自动重算后继续，也不让 Agent 静默修复冲突。merge 失败必须保持目标分支
原状。阶段 3 不自动 push；push、PR 和远程凭据是后续独立危险能力。

### 阶段 4：强隔离执行

把需要代码执行的能力迁入容器或远程 sandbox，默认断网且不注入宿主秘密。增加网络、依赖安装和外部
资源的单独审批，以及 CPU、内存、进程、磁盘、时间和 egress 配额。Agent Server 只持有 sandbox 引用，
不把 provider client 写入 checkpoint。

### 阶段 5：高级 Coding 工作流

在证据证明需要后增加验证失败修复循环、只读并行分析、独立 code review graph、长任务恢复和行为评测。
并行节点默认只读；任何写入仍通过单一顺序 mutation lane 和 digest-bound approval。

## 11. 测试策略

阶段 1 使用独立临时 RED/GREEN 目录：

- `tests/tdd/ai-coding-workspace/`：worktree 生命周期、身份隔离、路径逃逸、protected globs、TTL；
- `tests/tdd/ai-coding-patch/`：patch 解析、digest、基线漂移、dry apply、失败原子性；
- `tests/tdd/ai-coding-graph/`：coding 路由、interrupt、approve/reject/respond 和 resume 绑定。

这些测试使用临时 Git 仓库，强制 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、offline，不读取真实 `.env`，
不调用真实 Provider 或网络。它们不进入默认 pytest 收集，不自动晋升 core，用户可在功能完成后手动整目录
删除。

核心不变量影响：

- `LOOP-001` 当前只登记 fast/planning，增加 coding 路由时必须更新 invariant 文本和现有 runtime lifecycle
  测试；
- `CTX-001` 必须扩展为覆盖 coding patch 的强制 HITL；
- `RUN-001`、`TOOL-001`、`EXT-001` 和 `IDENT-001` 的现有原则保持不变；
- 不为 prompt、Tool description、完整自然语言输出或 UI 文案增加永久测试。

只有定向测试无法界定共享核心影响、修改登记 invariant 或进入发布评审时，才扩大验证范围。真实 Provider
验证永不进入 pytest。

## 12. 阶段 1 验收标准

1. 同一用户和 thread 重连后解析到同一个临时 worktree。
2. 不同用户或 thread 不能读取、审批或应用彼此 proposal。
3. 模型没有 shell、直接文件写入、删除、Git commit 或 merge 能力。
4. 路径逃逸、symlink escape 和 protected path 修改全部 fail closed。
5. 未审批、拒绝、digest 不匹配或基线漂移时 workspace 保持不变。
6. 批准有效 patch 后，全部目标文件一次性完成修改，不出现部分写入。
7. interrupt/resume 后不能重新解释或替换已批准 patch。
8. terminal state 返回 workspace ref、base commit、patch digest、changed paths 和 diff 摘要。
9. mock/offline 的 feature TDD 与受影响 core invariant 定向测试通过。
10. runtime、tool-calling、Agent Server authority、manifest 路由和 core invariant 与实现同步。

## 13. 后续计划边界

本规格覆盖完整五阶段方向，但实施计划必须按阶段拆分。第一份实施计划只覆盖阶段 1；阶段 2 至阶段 5
分别在上一阶段验收后建立独立计划，不能提前把 shell、merge 或远程副作用混入阶段 1。

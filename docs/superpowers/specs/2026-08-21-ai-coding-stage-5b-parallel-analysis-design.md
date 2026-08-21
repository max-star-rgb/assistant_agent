# AI Coding Stage 5B：只读并行分析设计

日期：2026-08-21

> 本文是 Stage 5B 的开发设计材料，不是当前架构权威。实现完成后的事实必须同步到对应根级 authority。

## 1. 目标与范围

Stage 5B 在现有唯一 `AssistantCodingGraph` 内增加一次初始只读分析 super-step。三个固定分析任务并行读取
同一不可变 workspace snapshot，产出有界、结构化、可追踪的分析结果；确定性 join 汇总这些结果后，将其作为
一次性临时 context 交给现有唯一 `inspect_and_draft`。只有该顺序节点能够生成 patch proposal，后续 patch
validation、digest-bound HITL、apply、dependency/credential/artifact gates、validation repair 和 integration
控制流保持不变。

本阶段不创建第二套 Runtime、独立可写 Graph、新 run、后台任务或并行 mutation lane。分析结果是 advisory
evidence，不具备授权、审批、写入、命令执行、网络访问或治理策略选择能力。

repository 通过服务端静态配置 `parallel_analysis_enabled` 显式启用，默认关闭。关闭时 Graph 行为与 Stage 5A
保持兼容。

## 2. 设计依据

LangGraph Graph API 将同一 super-step 中的多个节点定义为并行执行，并提供 `Send` 作为动态 map-reduce
派发原语。Stage 5B 复用项目 planning graph 已采用的原生 `Send` 与 reducer 模式，不自建线程池、任务队列或
并行调度协议。

LangGraph checkpoint 位于 super-step 边界，失败恢复可能从节点函数开头重新执行。因此分析 worker 必须无
副作用，结果必须绑定稳定 task ID，并由 reducer 去重。参考：

- <https://docs.langchain.com/oss/python/langgraph/graph-api#send>
- <https://docs.langchain.com/oss/python/langgraph/graph-api#re-execution-and-idempotency>

## 3. Graph 拓扑

启用 Stage 5B 时，初始控制流为：

```text
resolve_workspace
  -> prepare_analysis
  -> Send(analyze_workspace) x 3
  -> join_analysis
  -> inspect_and_draft
  -> validate_proposal
  -> existing sequential mutation lane
```

关闭 Stage 5B 时继续使用：

```text
resolve_workspace -> inspect_and_draft
```

`prepare_analysis` 是确定性节点。它解析 repository 静态配置、冻结 snapshot、创建三个固定 task，并清除同一
新分析周期的旧结果。router 只读取 repository 配置和结构化 state，不从用户文本、关键词、Memory 或 Tool 输出
推断是否启用分析。

conditional edge 返回三个 `Send("analyze_workspace", worker_state)`。每个 worker 获得不同 task 和同一个
snapshot ref。worker 结束后通过 reducer 写入 `analysis_results`，随后 `join_analysis` 统一验证、排序、去重和
裁剪。join 不调用模型。

Stage 5B 只在首次 draft 前运行一次。以下路径不得重新进入分析：

- validation repair 的 `prepare_repair -> consume_repair_budget -> inspect_and_draft`；
- formatter approval 的 `respond` 回路；
- patch、dependency、credential、artifact 和 merge interrupt/resume；
- 已完成分析的 checkpoint 恢复。

## 4. 固定分析任务

本阶段只提供三个内置 task，不允许模型、用户文本或 repository 内容动态增加 task：

1. `structure_context`：识别相关模块、接口、数据流和现有实现模式。
2. `change_test_impact`：识别潜在变更面、测试入口、兼容性和回归风险。
3. `safety_governance`：识别权限、凭据、网络、路径、持久化、HITL 和治理边界风险。

每个 task 的 ID、维度、system instruction、最大 findings、模型调用预算和只读 Tool inventory 由生产代码静态
定义。固定 task 只是通用分析视角，不包含业务关键词路由。

每个 worker 复用同一份 compiled read-only `create_agent`。该 agent 只暴露 snapshot-bound 版本的：

```text
coding_repo_list
coding_repo_search
coding_repo_read
coding_repo_status
coding_repo_diff
```

不得暴露 `coding_propose_patch`、validation command、dependency/artifact fetch、credential、apply、Git
integration、通用 shell 或网络 Tool。Tool exposure 由静态装配决定，不由 prompt 约束代替。

## 5. Snapshot 与读取一致性

`prepare_analysis` 通过 `CodingWorkspaceService` 创建受管、内容寻址、只读的 analysis snapshot。snapshot 必须
准确覆盖创建时当前 worktree 的累计内容，包括受现有 policy 允许的已跟踪修改和新增文本文件；实现不得修改真实
Git index 或 worktree。

snapshot contract 包含：

```python
class CodingAnalysisSnapshot(BaseModel):
    snapshot_ref: str
    workspace_ref: str
    base_commit: str
    tree_digest: str
    workspace_diff_digest: str
    created_at: datetime
    expires_at: datetime
```

`snapshot_ref` 是 opaque 引用，按认证身份、thread、workspace 和 repository 绑定。Graph state/checkpoint 不保存
snapshot 宿主路径、文件句柄、Git process、临时 index 或 backend client。

分析专属 read Tool 必须从 snapshot backend 读取，而不是从可变 worktree 读取。现有路径规范、protected globs、
symlink escape、普通文件、UTF-8、大小、窗口、搜索数量和 Tool 输出限制全部复用，不得因为 snapshot 是只读的而
放宽内容访问策略。

所有 result 必须回显同一 `snapshot_ref` 与 `tree_digest`。`join_analysis` 遇到 snapshot 不匹配、过期、身份不匹配
或结果 contract 无效时，不把对应内容交给 primary inspect。身份和访问控制错误 fail closed；普通 stale result
记录结构化失败。

snapshot 由 Agent Server process owner 管理并带 TTL。正常 join 后可释放 active lease；清理失败进入受管 TTL
reaper 和脱敏审计。snapshot 不包含凭据或外部 artifact，但仍按源码敏感资产执行身份隔离和期限清理。

## 6. State 与结构化契约

新增严格、冻结、JSON-safe 的 Pydantic model：

```python
class CodingAnalysisTask(BaseModel):
    task_id: str
    dimension: Literal[
        "structure_context",
        "change_test_impact",
        "safety_governance",
    ]
    objective: str
    allowed_tool_names: tuple[str, ...]

class CodingAnalysisFinding(BaseModel):
    finding_id: str
    category: str
    severity: Literal["info", "low", "moderate", "important"]
    summary: str
    path: str | None
    start_line: int | None
    end_line: int | None
    evidence_digest: str

class CodingAnalysisResult(BaseModel):
    task_id: str
    snapshot_ref: str
    tree_digest: str
    status: Literal["succeeded", "failed", "stale"]
    findings: tuple[CodingAnalysisFinding, ...]
    covered_paths: tuple[str, ...]
    output_digest: str
    error_code: str | None
```

字段使用严格长度、数量、pattern 和 tuple validator。finding 不保存完整文件、完整 diff、Provider 原始响应、异常
正文、宿主路径或 secret。`finding_id` 和 `output_digest` 由受信 worker adapter 根据规范化结果计算，不接受模型
自报值作为事实。

`CodingState` 增加：

```python
analysis_snapshot: CodingAnalysisSnapshot | None
analysis_tasks: tuple[CodingAnalysisTask, ...]
analysis_results: Annotated[list[CodingAnalysisResult], merge_analysis_results]
analysis_status: Literal["pending", "completed", "partial", "unavailable"] | None
```

`merge_analysis_results` 按稳定 `task_id` 合并，避免 checkpoint 重放重复追加。join 始终按固定 task 顺序输出，不依赖
并发完成顺序。

## 7. 模型输入与结果汇聚

worker 输入只包含：

- 当前 coding 请求的有界标准 messages 投影；
- 固定 task objective；
- snapshot 的非敏感摘要；
- 明确的只读分析输出 contract。

task instruction 作为本次调用的临时消息使用。worker 的 AI/Tool transcript、task instruction 和原始 structured
response 不追加到主 Graph 的 `messages` channel。worker adapter 只返回校验后的 `CodingAnalysisResult`。

`join_analysis` 执行：

1. 校验三个 task ID、snapshot ref、tree digest 和 contract。
2. 按 task 固定顺序和 finding ID 排序。
3. 对相同 path/category/evidence digest 的结果确定性去重。
4. 每个 task 最多保留 12 个 finding、6,000 个渲染字符。
5. 总临时 analysis context 最多 24,000 个渲染字符。
6. 使用稳定截断标记和完整结果 digest，禁止输出半个 JSON 或半条 finding。

primary `inspect_and_draft` 在首次调用时把汇总结果渲染为一次性临时 `HumanMessage`，不写回标准 messages。
primary 仍必须使用实时 workspace read Tool 自行确认内容；分析结果标记为 advisory，不可视为授权事实。只有 primary
拥有 `coding_propose_patch`。

## 8. 并发、预算与重放

并发数固定为 3。每个 worker 使用独立的 model/tool call limit，不能借用其他 worker 的剩余预算。总 task 数、
finding 数、内容长度、read bytes 和搜索结果均有服务端硬限制。

`analyze_workspace` 使用原生 node `RetryPolicy` 只重试明确的 timeout、connection 和临时 Provider HTTP 错误。
取消、interrupt、身份错误、snapshot 访问错误、schema/programming error 和权限错误不进入普通 retry/fallback。

worker 不产生外部副作用。节点重放可能再次调用模型，但 reducer 只保留同一 task ID 的一个已提交结果；结果内容
digest 始终对应实际进入 checkpoint 的规范化结果。分析不承担 exactly-once 外部操作语义。

## 9. 错误与降级

稳定错误至少包括：

- `coding_analysis_snapshot_failed`
- `coding_analysis_snapshot_expired`
- `coding_analysis_snapshot_mismatch`
- `coding_analysis_identity_mismatch`
- `coding_analysis_contract_invalid`
- `coding_analysis_task_failed`
- `coding_analysis_unavailable`

单个普通 Provider/Tool 临时错误在重试耗尽后成为 status=`failed` 的结果。至少一个 task 成功时 join 设置
`analysis_status="partial"` 或 `completed`，primary inspect 继续执行。三个 task 全部普通失败时设置
`analysis_status="unavailable"`，primary 仍可依靠现有实时 read Tool 工作。

这种降级只适用于 advisory 分析质量，不适用于身份、snapshot 隔离、Tool exposure、路径访问或 Graph contract。
安全边界错误 fail closed。分析结果 severity 不触发自动拒绝、自动审批、Tool 扩权或治理 gate 变化。

## 10. 配置与生命周期

repository 静态配置新增：

```text
parallel_analysis_enabled: bool = false
```

本阶段不开放用户提供 task、prompt、并发数、Tool 名、snapshot TTL 或预算。operator 后续若需要调节预算，必须通过
服务端有界配置完成，不接受 run input 覆盖。

Graph 从 START 恢复时：

- 已有 terminal `coding_result` 时直接 summarize；
- `analysis_status` 已完成、部分完成或 unavailable 时不得重复创建 snapshot；
- active repair 继续先经过既有 repair budget gate；
- pending analysis 使用 checkpoint 中的 snapshot/task/result 恢复未完成 super-step；
- snapshot 已过期或 digest 不匹配时不静默重建并复用旧结果，必须结构化终止或按尚未开始的新周期明确重建。

## 11. 安全边界

Stage 5B 保持以下硬边界：

- 并行节点全部只读，任何 mutation 仍位于唯一顺序 lane。
- 不允许分析 agent proposal patch、执行 validation、安装依赖、获取 artifact 或租用 credential。
- 不允许网络、任意 shell、Git commit/merge/push 或 PR。
- 不从分析结果、用户关键词或模型声明改变 Tool exposure、repository profile 或审批策略。
- 不把 snapshot backend、Provider client、进程对象或完整 transcript 写入 checkpoint。
- 不因分析失败绕过现有 patch validation、HITL、dependency/credential/artifact gates 或最终 validation。

## 12. 测试策略

临时 RED/GREEN 位于 `tests/tdd/ai-coding-parallel-analysis/`，可由用户手动整目录删除，不自动晋升 core。
使用 mock/offline、临时 Git repository 和确定性 fake agent，不调用真实 Provider 或网络。

覆盖：

1. repository 默认关闭时保持 Stage 5A 原拓扑与行为。
2. 启用时三个固定 task 通过 `Send` 在同一 super-step 派发。
3. barrier fake 证明 worker 可并发，而不是顺序调用。
4. 三个 worker 读取同一 snapshot，实时 worktree 后续变化不会改变 snapshot 内容。
5. snapshot Tool inventory 不包含 propose、command、credential、artifact 或 integration 能力。
6. reducer 按 task ID 去重，join 输出不依赖完成顺序。
7. worker transcript 不进入主 messages，primary 只获得有界临时 context。
8. 单 task failure、全部普通 failure、stale result 和 contract invalid 的边界正确。
9. cancel、interrupt、身份和权限错误不被普通降级吞掉。
10. context、finding、文件窗口、搜索结果和 Tool/model calls 全部有界。
11. repair、formatter respond 和各种 approval resume 不重复运行 analysis。
12. primary inspect 仍是唯一 proposal 生产者，现有 approval/apply/gates/validation/integration 不变。

本阶段改变 `LOOP-001`：更新现有 invariant 文本和最小 runtime lifecycle 断言，不新建 core 文件。实现涉及当前
authority 时同步更新 `docs/runtime-event-stream-architecture.md`、`docs/agent-server-architecture.md` 和
`docs/authority.toml`，并运行 authority validator。

## 13. 验收标准

1. 三个固定分析任务在一个原生 LangGraph super-step 并行执行。
2. 所有 worker 只读取同一个不可变、身份绑定的 snapshot。
3. 分析 Tool inventory 不含任何写入、命令、网络或集成能力。
4. 分析结果严格、有界、结构化、可去重且不污染主 messages。
5. 单个普通分析失败不阻断 primary，原生 cancel/interrupt 不被吞掉。
6. primary `inspect_and_draft` 是唯一 patch proposal 生产者。
7. 所有 mutation 继续经过现有 validator、digest-bound HITL、治理 gates、validation 和 integration。
8. repair、formatter 和 resume 不重复启动分析。
9. 默认关闭时与 Stage 5A 行为兼容。
10. feature TDD、受影响 core、历史 coding 回归和 authority validator 通过。

## 14. 非目标

Stage 5B 不包含独立 code review graph、自动 review verdict、并行写入、多候选 patch 竞争、跨 run 长任务恢复、
行为评测、自动 push/PR、冲突自动修复、基础设施故障自愈、任意命令、validation 网络访问或免审批 patch。
这些能力继续拆分为后续 Stage 5 子阶段。

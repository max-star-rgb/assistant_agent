# 原生 Agent 父图收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 将生产主链从“LangGraph + 自研 Runtime/facade”迁移为 Agent Server 托管的统一父 StateGraph；fast 分支与 planning worker 复用同一个 create_agent，planning 保留显式业务拓扑。

**Architecture:** 新建 assistant_agent.native_agent 包，先建立标准 state/context、BaseChatModel、LangChain Tool 与 Memory backend，再组装 fast Agent、planning 子图和父图。切换 langgraph.json 与 /agent-service/v1 后，旧生产主链只读退出；外围 CLI、A2A、automation、durable task 暂不迁移。

**Tech Stack:** Python 3.11、LangGraph 1.2.x、LangChain 1.3.x、LangChain Core 1.5.x、LangGraph SDK 0.4.x、Pydantic 2、LangMem、Mem0、MCP Python SDK、langchain-mcp-adapters 0.3.x、pytest。

## Global Constraints

- 实施前使用 superpowers:using-git-worktrees 从当前 HEAD 创建隔离 worktree；原工作区的旧主链未提交 diff 保持原样，不复制到新 worktree。
- 生产只有一个 Agent Server、一个父 StateGraph 和一套 thread/run/checkpoint/Store 生命周期。
- execution_mode 只接受结构化 fast 或 planning，不得从用户文本、关键词、Skill 或 Tool 结果推断。
- create_agent 只负责 fast ReAct 与 planning worker；Memory、模式路由、planner、join、verifier、repair 由显式 StateGraph 负责。
- Provider、Store 和认证对象只放 Runtime.context；checkpoint 只保存标准 messages、冻结 Memory 和规划业务事实。
- 旧 AssistantTurnState checkpoint 只读归档；新 Graph 使用新 assistant 版本与新 thread，不实现 schema migration。
- /agent-service/v1 保留为 Media-Agent wire 与 langgraph_sdk 的薄适配器。
- 默认验证必须设置 MULTIMODAL_AGENT_PROVIDER_MODE=mock，不得读取真实凭据或调用真实 Provider/Memory 服务。
- 每个任务只提交该任务列出的文件；不得回滚原工作区已有修改，不得 push、merge 或创建 PR。
- 临时 RED/GREEN 测试统一位于 tests/tdd/native-agent-parent-graph/，用户可在功能完成后手动整目录删除。

---

## 文件结构

新主链集中在一个新包，避免继续扩张旧 runtime：

    src/assistant_agent/native_agent/
      __init__.py          # 新父图公共构建入口
      state.py             # Root/fast/planning/worker state 与 reducer
      context.py           # 可信 run context
      models.py            # 规划、worker、verification 严格 DTO
      providers.py         # BaseChatModel composition root
      tools.py             # LangChain Tool 与官方 MCP 装配
      memory.py            # 最小 MemoryBackend 与固定节点
      fast_agent.py        # create_agent 组装
      planning_graph.py    # planner/Send/worker/join/verifier/repair/finalize
      root_graph.py        # memory -> mode -> branch -> memory commit

旧模块在新主链验证前继续服务外围入口。最终清理只删除已经没有 import consumer 的生产 facade，不提前删除外围依赖。

## 测试策略决定

本次会改变已登记的 RUN-001、LOOP-001、TOOL-001、EXT-001、MEMORY-001、CTX-001、GATE-001 和 OBS-001，因此最终必须修改这些 ID 的现有负责测试；不新增永久 core 文件。功能开发的 RED/GREEN 全部进入 tests/tdd/native-agent-parent-graph，且不得自动晋升 core。

### Task 1: 建立依赖、Root/Planning state 与确定性 reducer

**Files:**
- Modify: pyproject.toml
- Create: src/assistant_agent/native_agent/__init__.py
- Create: src/assistant_agent/native_agent/state.py
- Create: src/assistant_agent/native_agent/context.py
- Create: src/assistant_agent/native_agent/models.py
- Test: tests/tdd/native-agent-parent-graph/test_state_channels.py

**Interfaces:**
- Produces: AssistantRootState、AssistantRootInput、FastAgentState、PlanningState、WorkerState、AssistantRunContext、merge_worker_results()、merge_artifacts()。
- Consumes: LangChain AgentState/add_messages；不消费 AssistantTurnState。

- [ ] **Step 1: 补充直接依赖**

在 dependencies 加入以下精确范围：

    "langchain>=1.3.15,<2",
    "langchain-core>=1.5.4,<2",
    "langgraph-sdk>=0.4.2,<0.5",
    "langchain-mcp-adapters>=0.3.2,<0.4",

运行：

    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e .

预期：依赖解析成功，langchain_mcp_adapters 可导入。

- [ ] **Step 2: 写 reducer RED 测试**

测试 messages 追加、相同 worker/artifact ID 的相同内容幂等、不同内容冲突、completed ID 排序稳定，以及 execution_mode 只接受 fast/planning。

    def test_worker_result_conflict_fails_closed() -> None:
        left = {"node-a": WorkerResult(work_item_id="node-a", content="v1")}
        right = {"node-a": WorkerResult(work_item_id="node-a", content="v2")}
        with pytest.raises(ValueError, match="worker result conflict"):
            merge_worker_results(left, right)

- [ ] **Step 3: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_state_channels.py

预期：因 native_agent.state 尚未实现而失败。

- [ ] **Step 4: 实现最小 state/context**

    class AssistantRootState(AgentState):
        execution_mode: Required[Literal["fast", "planning"]]
        memory_context: NotRequired[tuple[str, ...]]
        memory_status: NotRequired[Literal["ready", "empty", "degraded"]]

    class AssistantRootInput(TypedDict):
        messages: Required[Annotated[list[AnyMessage], add_messages]]
        execution_mode: Required[Literal["fast", "planning"]]

    class PlanningState(AgentState):
        memory_context: Required[tuple[str, ...]]
        plan: NotRequired[WorkflowPlanV2Proposal]
        worker_results: Annotated[dict[str, WorkerResult], merge_worker_results]
        completed_work_item_ids: Annotated[tuple[str, ...], merge_sorted_ids]
        artifacts: Annotated[dict[str, PlanningArtifact], merge_artifacts]
        verification: NotRequired[VerificationResult]
        repair_count: NotRequired[int]

    class AssistantRunContext(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
        user_id: str
        tenant_id: str
        entry_profile: str = "agent_server"
        media_capabilities: tuple[str, ...] = ()

PlanningState.worker_results 和 artifacts 使用冲突检测 reducer；repair_count 普通 overwrite；model/client/store 不进入 state。

- [ ] **Step 5: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_state_channels.py
    git add pyproject.toml src/assistant_agent/native_agent tests/tdd/native-agent-parent-graph/test_state_channels.py
    git commit -m "feat: define native agent graph state"

### Task 2: 将主 Chat Provider 收敛为标准 BaseChatModel

**Files:**
- Create: src/assistant_agent/native_agent/providers.py
- Modify: src/assistant_agent/providers/dashscope_chat.py
- Test: tests/tdd/native-agent-parent-graph/test_chat_models.py

**Interfaces:**
- Consumes: ProviderConfig、现有 OpenAI-compatible/DashScope HTTP 传输和安全错误清洗。
- Produces: create_chat_model(config: ProviderConfig) -> BaseChatModel；mock、unconfigured、OpenAI-compatible、DashScope 均返回标准 AIMessage/AIMessageChunk。

- [ ] **Step 1: 写 BaseChatModel RED 测试**

覆盖 invoke/ainvoke、stream/astream、bind_tools、usage metadata、tool call ID/arguments、mock 确定性、real mode 缺配置 fail closed。

    def test_mock_chat_model_returns_standard_ai_message() -> None:
        model = create_chat_model(ProviderConfig(provider_mode="mock"))
        result = model.invoke([HumanMessage(content="sentinel")])
        assert isinstance(result, AIMessage)
        assert result.response_metadata["provider"] == "mock"

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_chat_models.py

- [ ] **Step 3: 实现标准模型适配**

    class MockAssistantChatModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "assistant-agent-mock"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            message = AIMessage(content=deterministic_mock_text(messages))
            return ChatResult(generations=[ChatGeneration(message=message)])

实现前联网核对 DeepSeek、阿里百炼和火山 Ark 当前官方 chat/tool-call/stream 协议，并把核对日期和链接记录在提交说明。真实 adapter 把 citation、reasoning、search source 放入标准 content block 或 response_metadata；不得恢复 LLMEvent 到 AgentEvent 投影。bind_tools 必须保留 LangChain schema 并映射供应商 wire。

- [ ] **Step 4: 运行 GREEN、compileall 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_chat_models.py
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/native_agent src/assistant_agent/providers
    git add src/assistant_agent/native_agent/providers.py src/assistant_agent/providers/dashscope_chat.py tests/tdd/native-agent-parent-graph/test_chat_models.py
    git commit -m "feat: expose chat providers as base chat models"

### Task 3: 建立 LangChain Tool 与官方 MCP 静态装配

**Files:**
- Create: src/assistant_agent/native_agent/tools.py
- Modify: src/assistant_agent/mcp/config.py
- Test: tests/tdd/native-agent-parent-graph/test_native_tools.py

**Interfaces:**
- Produces: create_native_tools(config, resources) -> list[BaseTool]；create_mcp_tools(config) -> list[BaseTool]。
- Consumes: 当前 built-in Tool builder、ToolResult 和结构化 MCP 配置；不消费 Registry/Validator/Executor。

- [ ] **Step 1: 写 Tool RED 测试**

证明返回对象都是 BaseTool、Pydantic schema 由 LangChain 校验、ToolRuntime 注入身份、结果使用 content_and_artifact，主链接口没有 Registry/Validator/Executor 参数。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_native_tools.py

- [ ] **Step 3: 实现本地 Tool 静态清单**

显式列出受信 built-in builders，不读取 configured Python plugin module。每个 concrete Tool 包装为 StructuredTool：

    async def invoke_tool(runtime: ToolRuntime[AssistantRunContext], **payload):
        result = await asyncio.to_thread(
            concrete_tool.run,
            payload,
            ToolContext(
                user_id=runtime.context.user_id,
                session_id=runtime.execution_info.thread_id,
                run_id=runtime.execution_info.run_id,
            ),
        )
        return result.model_observation or result.error or result.data, result.data

使用 StructuredTool.from_function(args_schema=..., response_format="content_and_artifact")。副作用幂等由具体 Tool 或业务 API 承担。

- [ ] **Step 4: 使用官方 MCP client**

把受信配置转换为 MultiServerMCPClient 配置并调用 await client.get_tools()；保留 allowlist 和 namespacing，不建立 MCP proxy/ToolSpec/Registry。

- [ ] **Step 5: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_native_tools.py
    git add src/assistant_agent/native_agent/tools.py src/assistant_agent/mcp/config.py tests/tdd/native-agent-parent-graph/test_native_tools.py
    git commit -m "feat: assemble native langchain tools"

### Task 4: 将 Memory 改为最小 backend 与固定节点

**Files:**
- Create: src/assistant_agent/native_agent/memory.py
- Test: tests/tdd/native-agent-parent-graph/test_native_memory.py

**Interfaces:**
- Produces: MemoryBackend.recall/commit、create_memory_backend、memory_recall_node、memory_recall_degraded、memory_commit_node。
- Consumes: AssistantRootState、AssistantRunContext、runtime.store。

- [ ] **Step 1: 写 Memory RED 测试**

覆盖 disabled、LangMem、Mem0、第三方 probe adapter；同一 run recall/commit 各一次；recall 最终失败为 degraded；commit 失败不删除最终 AIMessage；worker 不持有 backend。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_native_memory.py

- [ ] **Step 3: 实现最小协议与节点**

    class MemoryBackend(Protocol):
        async def recall(self, *, context, thread_id, run_id, messages, store) -> tuple[str, ...]: ...
        async def commit(self, *, context, thread_id, run_id, messages, store) -> None: ...

新模块直接复用薄 Mem0Client 和 LangMem 官方 manager，不调用旧 create_memory_node_bundle，也不修改旧 backend。新路径不得构造 SQLiteMemoryCommitLedger。Mem0 使用 Agent Server run identity 作为后端 idempotency key；LangMem 使用 runtime.store；第三方 adapter 只实现该协议。

- [ ] **Step 4: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_native_memory.py
    git add src/assistant_agent/native_agent/memory.py tests/tdd/native-agent-parent-graph/test_native_memory.py
    git commit -m "feat: add native graph memory boundary"

### Task 5: 构建可复用 fast create_agent

**Files:**
- Create: src/assistant_agent/native_agent/fast_agent.py
- Test: tests/tdd/native-agent-parent-graph/test_fast_agent.py

**Interfaces:**
- Consumes: BaseChatModel、list[BaseTool]、FastAgentState、AssistantRunContext。
- Produces: build_fast_agent(...) -> CompiledStateGraph，graph name 为 AssistantFastAgent。

- [ ] **Step 1: 写 fast Agent RED 测试**

验证纯文本、标准 tool call/ToolMessage、原生 message stream、Memory prompt 注入、model/tool call limit、只读 Tool retry、summarization，以及受信 write Tool HITL；测试不得 import 旧 assistant loop。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_fast_agent.py

- [ ] **Step 3: 使用 create_agent 实现**

    @dynamic_prompt
    def assistant_prompt(request: ModelRequest) -> str:
        memories = request.state.get("memory_context", ())
        return render_minimal_system_prompt(memories, request.runtime.context)

    return create_agent(
        model=model,
        tools=tools,
        state_schema=FastAgentState,
        context_schema=AssistantRunContext,
        middleware=[
            assistant_prompt,
            ModelCallLimitMiddleware(run_limit=model_call_limit, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=tool_call_limit, exit_behavior="error"),
            ToolRetryMiddleware(max_retries=2, tools=read_tool_names),
            SummarizationMiddleware(
                model=model,
                trigger=("fraction", 0.7),
                keep=("fraction", 0.4),
            ),
            HumanInTheLoopMiddleware(interrupt_on=trusted_interrupt_policy),
        ],
        name="AssistantFastAgent",
    )

fast Agent 不绑定 saver/store，作为父图子图继承 Agent Server 资源。

- [ ] **Step 4: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_fast_agent.py
    git add src/assistant_agent/native_agent/fast_agent.py tests/tdd/native-agent-parent-graph/test_fast_agent.py
    git commit -m "feat: build reusable fast agent"

### Task 6: 迁移 planning 业务拓扑并复用 fast Agent

**Files:**
- Create: src/assistant_agent/native_agent/planning_graph.py
- Modify: src/assistant_agent/native_agent/models.py
- Test: tests/tdd/native-agent-parent-graph/test_planning_graph.py

**Interfaces:**
- Consumes: 同一个 fast_agent compiled object、WorkflowPlanV2Proposal 和现有 DAG admission 纯函数。
- Produces: build_planning_graph(model, fast_agent) -> CompiledStateGraph，graph name 为 AssistantPlanningGraph。

- [ ] **Step 1: 写 planning RED 测试**

覆盖结构化 planner、DAG admission、Send fan-out、稳定 ID join、worker 使用同一个 fast Agent、verifier pass、局部 repair、repair 上限、finalize 标准 AIMessage。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_planning_graph.py

- [ ] **Step 3: 实现显式 StateGraph**

    builder.add_node("planner", planner_node, retry_policy=RetryPolicy(max_attempts=2))
    builder.add_node("worker", worker_subgraph)
    builder.add_node("join", join_node)
    builder.add_node("verifier", verifier_node, retry_policy=RetryPolicy(max_attempts=2))
    builder.add_node("repair", repair_node)
    builder.add_node("finalize", finalize_node)

dispatch 返回 Send("worker", WorkerState(...))。worker subgraph 内加入 Task 5 的同一个 fast_agent 对象。planner/verifier 使用 model.with_structured_output；repair 只写 repair_count 新值并重新派发指定节点。

- [ ] **Step 4: 运行并行、resume/replay 确定性 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_planning_graph.py
    git add src/assistant_agent/native_agent/planning_graph.py src/assistant_agent/native_agent/models.py tests/tdd/native-agent-parent-graph/test_planning_graph.py
    git commit -m "feat: add native planning subgraph"

### Task 7: 组装统一父 StateGraph

**Files:**
- Create: src/assistant_agent/native_agent/root_graph.py
- Modify: src/assistant_agent/native_agent/__init__.py
- Test: tests/tdd/native-agent-parent-graph/test_root_graph.py

**Interfaces:**
- Consumes: Memory nodes、fast Agent、planning graph。
- Produces: build_assistant_root_graph(...)；唯一拓扑为 memory_recall -> fast/planning -> memory_commit。

- [ ] **Step 1: 写父图 RED 测试**

验证结构化模式路由、普通文本不能改模式、两分支各 recall/commit 一次、planning worker 不重复 recall、最终只有标准 messages、未知模式输入校验失败。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_root_graph.py

- [ ] **Step 3: 实现父图**

    builder = StateGraph(
        AssistantRootState,
        input_schema=AssistantRootInput,
        context_schema=AssistantRunContext,
    )
    builder.add_node(
        "memory_recall",
        memory_recall_node,
        retry_policy=RetryPolicy(max_attempts=3),
        error_handler=memory_recall_degraded,
    )
    builder.add_node("fast_agent", fast_agent)
    builder.add_node("planning_graph", planning_graph)
    builder.add_node("memory_commit", memory_commit_node)
    builder.add_edge(START, "memory_recall")
    builder.add_conditional_edges("memory_recall", route_execution_mode)
    builder.add_edge("fast_agent", "memory_commit")
    builder.add_edge("planning_graph", "memory_commit")
    builder.add_edge("memory_commit", END)

route_execution_mode 只能读取 state.execution_mode。memory_recall_node 从 runtime.execution_info 提取 thread_id/run_id；三次失败后由 error handler 返回 memory_context=() 和 memory_status="degraded"。memory_commit_node 捕获后端异常并只记录 LangSmith span，不改写 messages。

- [ ] **Step 4: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_root_graph.py
    git add src/assistant_agent/native_agent/root_graph.py src/assistant_agent/native_agent/__init__.py tests/tdd/native-agent-parent-graph/test_root_graph.py
    git commit -m "feat: compose native assistant parent graph"

### Task 8: 切换 Agent Server factory 与新 assistant 版本

**Files:**
- Modify: src/assistant_agent/agent_server/graph.py
- Modify: src/assistant_agent/agent_server/services.py
- Modify: src/assistant_agent/agent_server/context.py
- Modify: langgraph.json
- Test: tests/tdd/native-agent-parent-graph/test_agent_server_factory.py

**Interfaces:**
- Consumes: create_chat_model、native tools、Memory backend、root graph。
- Produces: assistant_agent.agent_server.graph:native_assistant_graph；assistant ID 为 assistant-native-v1。

- [ ] **Step 1: 写 factory RED 测试**

证明 factory 只构造新 composition、从认证 context 建立 AssistantRunContext、不实例化 AgentGraphRuntime/ToolExecutor/ProductEventProjector，并拒绝旧 state 输入。

- [ ] **Step 2: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_agent_server_factory.py

- [ ] **Step 3: 改写 composition owner**

AgentServerExecutionOwner 只拥有 model、tools/MCP client、Memory backend 与 close hooks。factory 不显式绑定 saver/store，交给 ServerRuntime 注入。AgentServerRunContext 只保存认证身份、tenant、entry profile 和媒体 capability；execution_mode 只存在于 AssistantRootInput，并由严格输入 schema 校验。

- [ ] **Step 4: 切换 manifest**

    "graphs": {
      "assistant-native-v1": "assistant_agent.agent_server.graph:native_assistant_graph"
    }

保留 auth 和 custom HTTP app；不注册旧 graph alias，避免旧 thread 被新图读取。

- [ ] **Step 5: 运行 GREEN、import smoke 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_agent_server_factory.py
    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -c 'from assistant_agent.agent_server.graph import native_assistant_graph'
    git add langgraph.json src/assistant_agent/agent_server/graph.py src/assistant_agent/agent_server/services.py src/assistant_agent/agent_server/context.py tests/tdd/native-agent-parent-graph/test_agent_server_factory.py
    git commit -m "feat: deploy native assistant parent graph"

### Task 9: 将 /agent-service/v1 收敛为薄 SDK adapter

**Files:**
- Modify: src/assistant_agent/agent_server/client.py
- Modify: src/assistant_agent/agent_server/media_protocol.py
- Modify: src/assistant_agent/agent_server/media_app.py
- Modify: src/assistant_agent/agent_server/media_session.py
- Test: tests/tdd/native-agent-parent-graph/test_media_native_adapter.py
- Modify: tests/core/contract/test_gateway_contract.py

**Interfaces:**
- Consumes: Agent Server messages/updates/run metadata stream。
- Produces: vendor assistantMode=fast|planning 到 graph execution_mode 的机械映射；assistant ID 固定 assistant-native-v1。

- [ ] **Step 1: 写媒体 adapter RED 测试**

覆盖新 mode、标准 HumanMessage 多模态 content block、原生 run cancel、断线 join、最终 AIMessage 投影；禁止 media session 保存 graph phase/checkpoint/自研 cancel token。

- [ ] **Step 2: 更新 GATE-001 既有测试**

改为断言 custom route 只调用 langgraph_sdk 的 thread/run/stream/cancel，输入是标准 messages 与结构化 execution_mode。不新增 core 文件。

- [ ] **Step 3: 运行 RED**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_media_native_adapter.py tests/core/contract/test_gateway_contract.py

- [ ] **Step 4: 实现薄投影**

    input={
        "messages": [{"role": "user", "content": content_blocks}],
        "execution_mode": chat.execution_mode,
    }

消费 messages stream，选择最新标准 AIMessage；错误直接使用原生 run error，不翻译成项目错误码。

- [ ] **Step 5: 运行 GREEN 并提交**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph/test_media_native_adapter.py tests/core/contract/test_gateway_contract.py
    git add src/assistant_agent/agent_server/client.py src/assistant_agent/agent_server/media_protocol.py src/assistant_agent/agent_server/media_app.py src/assistant_agent/agent_server/media_session.py tests/tdd/native-agent-parent-graph/test_media_native_adapter.py tests/core/contract/test_gateway_contract.py
    git commit -m "feat: adapt media service to native graph protocol"

### Task 10: 退休生产旧主链并同步 core invariant 与 authority

**Files:**
- Modify: AGENTS.md
- Modify: docs/gateway-architecture.md
- Modify: docs/runtime-event-stream-architecture.md
- Modify: docs/tool-calling-architecture.md
- Modify: docs/memory-service-architecture.md
- Modify: docs/context_engineering_status.md
- Modify: docs/authority.toml
- Modify: tests/core/INVARIANTS.md
- Modify: tests/core/integration/test_runtime_lifecycle.py
- Modify: tests/core/contract/test_tool_contract.py
- Modify: tests/core/contract/test_extension_contract.py
- Modify: tests/core/integration/test_memory_lifecycle.py
- Modify: tests/core/integration/test_context_lifecycle.py
- Modify: tests/core/contract/test_observability_contract.py

**Interfaces:**
- Consumes: 已切换并通过 TDD 的生产入口。
- Produces: 新主链唯一 authority；旧 Runtime/Workflow 文件原样留给外围入口，Agent Server graph/media composition 不再导入它们。

- [ ] **Step 1: 审计消费者**

    rg -n "AssistantTurnState|AgentGraphRuntime|WorkflowGraphHost|ProductEventProjector|ActionValidator|ToolExecutor" src/assistant_agent --glob '*.py'
    rg -n "assistant_agent\.runtime|assistant_agent\.workflows" src/assistant_agent/agent_server src/assistant_agent/native_agent

本任务不删除这些旧文件。将搜索结果保存为实施报告中的外围迁移清单，并确认 Agent Server 与 native_agent 的搜索结果为零；物理删除只进入后续外围迁移计划。

- [ ] **Step 2: 重写已改变的 core invariant**

- RUN-001：终态、cancel、resume 由 Agent Server/native checkpoint 所有。
- LOOP-001：父 StateGraph + fast create_agent + planning 子图。
- TOOL-001：主链标准 LangChain Tool schema/ToolRuntime；副作用幂等归具体 Tool/业务服务。
- EXT-001：静态本地 Tool + 官方 MCP adapter。
- MEMORY-001：父图固定 recall/commit 一次。
- CTX-001：标准 messages、dynamic prompt 与官方 middleware。
- GATE-001：保持 Agent Server lifecycle。
- OBS-001：LangSmith native tracing，不再要求 canonical 产品 lifecycle tree。

修改各 ID 已登记的现有负责测试，不新建重复永久测试。

- [ ] **Step 3: 同步 authority 与 AGENTS**

删除“所有 Tool 必须经过 ActionValidator/Executor/Registry”的全局规则，改为生产主链使用标准 LangChain Tool；write/dangerous Tool 在具体 Tool 或下游 API 内验证授权和幂等。文档明确 create_agent 不是父编排器，planning 仍是显式 StateGraph。

- [ ] **Step 4: 运行定向核心与全部临时 TDD**

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-agent-parent-graph tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_tool_contract.py tests/core/contract/test_extension_contract.py tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/contract/test_gateway_contract.py tests/core/contract/test_observability_contract.py

- [ ] **Step 5: 运行最终共享核心验证**

该任务改变多个登记 invariant，因此允许运行裸 pytest：

    MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
    git diff --check

预期：全部通过；authority validator 返回 errors: []；无网络和真实 Provider 调用。

- [ ] **Step 6: 提交退休与 authority 更新**

    git add AGENTS.md docs/authority.toml docs/gateway-architecture.md docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/memory-service-architecture.md docs/context_engineering_status.md tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_tool_contract.py tests/core/contract/test_extension_contract.py tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/contract/test_observability_contract.py
    git diff --cached --name-status
    git commit -m "refactor: retire production custom agent runtime"

在 commit 前确认 staged diff 不包含旧 runtime/workflows 源文件。

## 完成门槛

- Agent Server manifest 只注册 assistant-native-v1 父图。
- fast 与 planning 都返回标准 LangChain messages；planning worker 复用同一个 fast Agent。
- 每次 run 只有父图执行一次 Memory recall/commit。
- /agent-service/v1 只通过 langgraph_sdk 使用原生 thread/run/stream/cancel。
- 生产 graph/media composition 不导入 AgentGraphRuntime、AssistantTurnState、WorkflowGraphHost、ProductEventProjector、ActionValidator 或 ToolExecutor。
- 旧 checkpoint 未迁移；新 assistant 不读取旧 thread。
- 所有验证在 mock/offline 下通过；真实 Provider、Mem0、LangMem system eval 留待 operator 明确授权的发布评审。

## 后续独立计划

主链切换后，分别为 MCP server、A2A、automation/durable task、CLI/offline 编写迁移计划。只有这些入口全部退出旧 Runtime 后，才能删除整个旧 runtime、Registry/Plugin assembly、Workflow facade 与相关 SQLite schema。

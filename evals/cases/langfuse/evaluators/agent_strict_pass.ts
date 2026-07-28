/**
 * Langfuse Code Evaluator v1 for assistant_agent Agent Experiment outputs.
 *
 * Create an Experiment-targeted TypeScript Code Evaluator in Langfuse and paste
 * this source into it. The code executes inside Langfuse, not in assistant_agent.
 */
function evaluate({
  observation: { output },
  experiment,
}: EvaluationContext): EvaluationResult {
  const actual =
    typeof output === "string" ? JSON.parse(output) : output ?? {};
  const metadata = experiment?.itemMetadata ?? {};
  const capability = metadata.capability;
  const executions = Array.isArray(actual.tool_executions)
    ? actual.tool_executions
    : [];
  const availableTools = Array.isArray(actual.available_tools)
    ? actual.available_tools
    : [];
  const validationResults = Array.isArray(actual.validation_results)
    ? actual.validation_results
    : [];
  const traceNames = new Set(
    Array.isArray(actual.trace_event_names) ? actual.trace_event_names : [],
  );
  const commonTrace = [
    "run.completed",
    "response.final",
    "trace.content",
    "assistant.turn.summary",
  ];
  const executionChecks = {
    terminal_completed: actual.terminal_status === "completed",
    required_trace_present: commonTrace.every((name) => traceNames.has(name)),
  };
  const toolTraceEvents = [
    "action.validation.finished",
    "tool.started",
    "tool.observation",
  ];
  const toolChecks = {
    executed_tools_exposed: executions.every(
      (execution: any) =>
        execution.exposed === true &&
        Array.isArray(execution.exposed_tools) &&
        execution.exposed_tools.includes(execution.name) &&
        availableTools.includes(execution.name),
    ),
    validation_chain_accepted:
      validationResults.length === executions.length &&
      validationResults.every((result: any) => result.status === "accepted") &&
      executions.every((execution: any) =>
        validationResults.some(
          (result: any) =>
            result.tool_name === execution.name && result.status === "accepted",
        ),
      ),
    executions_reached_terminal: executions.every(
      (execution: any) =>
        ["tool.finished", "tool.failed"].includes(execution.terminal_event) &&
        ["succeeded", "failed"].includes(execution.status),
    ),
    tool_trace_complete:
      executions.length === 0 ||
      toolTraceEvents.every((name) => traceNames.has(name)),
  };
  const executionPassed = Object.values(executionChecks).every(Boolean);
  const toolChainPassed = Object.values(toolChecks).every(Boolean);
  return {
    scores: [
      booleanScore(
        "agent.runtime_trace_pass",
        executionPassed,
        "Runtime 正常结束，Trace 闭环证据完整",
        executionChecks,
      ),
      booleanScore(
        "agent.tool_mechanical_pass",
        toolChainPassed,
        executions.length === 0
          ? "本次没有工具调用，治理链路保持闭合"
          : "已发生工具调用的暴露、Validator、Trace 与结构化终态均正常",
        toolChecks,
        {
          capability,
          toolCallCount: executions.length,
          totalLatencyMs: actual.total_latency_ms ?? 0,
        },
      ),
    ],
  };
}

function booleanScore(
  name: string,
  value: boolean,
  passedResult: string,
  checks: Record<string, boolean>,
  metadata: Record<string, unknown> = {},
): any {
  const failedChecks = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => checkFailureDescription(name));
  return {
    name,
    value,
    dataType: "BOOLEAN",
    comment: value
      ? `通过：${passedResult}。`
      : `未通过：${failedChecks.join("；")}。`,
    metadata: {
      ...metadata,
      checks,
    },
  };
}

function checkFailureDescription(name: string): string {
  const descriptions: Record<string, string> = {
    terminal_completed: "Runtime 未正常结束",
    required_trace_present: "Trace 闭环证据不完整",
    executed_tools_exposed: "实际调用的工具未正确暴露",
    validation_chain_accepted: "工具调用未通过 Validator 或证据数量不一致",
    executions_reached_terminal: "至少一个工具调用缺少结构化执行终态",
    tool_trace_complete: "工具执行 Trace 证据不完整",
  };
  return descriptions[name] ?? `${name} 检查失败`;
}

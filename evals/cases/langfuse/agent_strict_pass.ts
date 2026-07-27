/**
 * Langfuse Code Evaluator for the assistant-agent-closed-loop-v1 Dataset.
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
  const expected = experiment?.itemExpectedOutput ?? {};
  const metadata = experiment?.itemMetadata ?? {};
  const capability = metadata.capability;
  const executions = Array.isArray(actual.tool_executions)
    ? actual.tool_executions
    : [];
  const toolNames = executions.map((execution: any) => execution.name);
  const requiredTools = Array.isArray(metadata.required_tools)
    ? metadata.required_tools
    : [];
  const forbiddenTools = Array.isArray(metadata.forbidden_tools)
    ? metadata.forbidden_tools
    : [];
  const responseText = actual.response?.message ?? "";
  const responseFacts = Array.isArray(expected.response_facts)
    ? expected.response_facts
    : [];
  const diff = actual.state_diff ?? {};
  const traceNames = new Set(
    Array.isArray(actual.trace_event_names) ? actual.trace_event_names : [],
  );
  const providerResultKinds = Array.isArray(actual.provider_result_kinds)
    ? actual.provider_result_kinds
    : [];
  const unusableProviderResults = new Set([
    "error",
    "refusal",
    "truncated",
    "empty",
  ]);
  const commonTrace = [
    "run.completed",
    "response.final",
    "trace.content",
    "assistant.turn.summary",
  ];
  const requiredToolSelectionMatches =
    capability === "real_read_only_tool"
      ? requiredTools.every((name: string) => toolNames.includes(name)) &&
        toolNames.every((name: string) => requiredTools.includes(name))
      : JSON.stringify(toolNames) === JSON.stringify(requiredTools);
  const checks: Record<string, boolean> = {
    terminal_completed: actual.terminal_status === "completed",
    response_present: responseText.length > 0,
    response_facts_present: responseFacts.every((fact: string) =>
      responseText.includes(fact),
    ),
    provider_results_usable:
      providerResultKinds.length > 0 &&
      !providerResultKinds.some((kind: string) =>
        unusableProviderResults.has(kind),
      ),
    required_trace_present: commonTrace.every((name) => traceNames.has(name)),
    required_tools_exact: requiredToolSelectionMatches,
    forbidden_tools_absent: !toolNames.some((name: string) =>
      forbiddenTools.includes(name),
    ),
    required_tools_exposed: requiredTools.every((name: string) =>
      (
        Array.isArray(actual.available_tools)
          ? actual.available_tools
          : []
      ).includes(name) || toolNames.includes(name),
    ),
  };

  if (capability === "write_tool") {
    const added = Array.isArray(diff.added) ? diff.added : [];
    const expectedEvent = expected.required_event ?? {};
    checks.target_event_created =
      added.length === 1 && objectContains(added[0], expectedEvent);
    checks.existing_state_preserved =
      emptyArray(diff.modified) &&
      emptyArray(diff.deleted) &&
      emptyArray(diff.duplicate_groups);
    checks.tool_succeeded =
      executions.length === 1 && executions[0].status === "succeeded";
    checks.validation_accepted = (
      Array.isArray(actual.validation_results)
        ? actual.validation_results
        : []
    ).some(
      (result: any) =>
        result.tool_name === "calendar_create" && result.status === "accepted",
    );
  } else if (capability === "read_only_tool") {
    const execution = executions[0] ?? {};
    const returnedEvents = execution.output?.data?.events ?? [];
    const expectedEvents = Array.isArray(expected.expected_events)
      ? expected.expected_events
      : [];
    checks.query_matches = execution.input?.query === expected.query;
    checks.tool_succeeded = execution.status === "succeeded";
    checks.expected_events_returned = expectedEvents.every(
      (event: Record<string, unknown>) =>
        returnedEvents.some((actualEvent: Record<string, unknown>) =>
          objectContains(actualEvent, event),
        ),
    );
    checks.state_unchanged =
      JSON.stringify(actual.initial_state) ===
        JSON.stringify(actual.final_state) &&
      emptyArray(diff.added) &&
      emptyArray(diff.modified) &&
      emptyArray(diff.deleted) &&
      emptyArray(diff.duplicate_groups);
  } else if (capability === "no_tool") {
    checks.no_tool_called = executions.length === 0;
    checks.state_unchanged =
      JSON.stringify(actual.initial_state) ===
        JSON.stringify(actual.final_state) &&
      emptyArray(diff.added) &&
      emptyArray(diff.modified) &&
      emptyArray(diff.deleted) &&
      emptyArray(diff.duplicate_groups);
  } else if (capability === "real_no_tool") {
    checks.no_tool_called = executions.length === 0;
    checks.state_unchanged =
      JSON.stringify(actual.initial_state) ===
        JSON.stringify(actual.final_state) &&
      emptyArray(diff.added) &&
      emptyArray(diff.modified) &&
      emptyArray(diff.deleted) &&
      emptyArray(diff.duplicate_groups);
  } else if (capability === "real_read_only_tool") {
    checks.tools_succeeded = requiredTools.every((name: string) =>
      executions.some(
        (execution: any) =>
          execution.name === name && execution.status === "succeeded",
      ),
    );
    checks.state_unchanged =
      JSON.stringify(actual.initial_state) ===
        JSON.stringify(actual.final_state) &&
      emptyArray(diff.added) &&
      emptyArray(diff.modified) &&
      emptyArray(diff.deleted) &&
      emptyArray(diff.duplicate_groups);
  } else if (capability === "real_write_tool") {
    checks.tools_succeeded = requiredTools.every((name: string) =>
      executions.some(
        (execution: any) =>
          execution.name === name && execution.status === "succeeded",
      ),
    );
  } else {
    checks.supported_capability = false;
  }

  const executionChecks = {
    terminal_completed: checks.terminal_completed,
    required_trace_present: checks.required_trace_present,
  };
  const toolChecks = {
    required_tools_exact: checks.required_tools_exact,
    required_tools_exposed: checks.required_tools_exposed,
    forbidden_tools_absent: checks.forbidden_tools_absent,
    tool_behavior_valid:
      checks.tool_succeeded ??
      checks.tools_succeeded ??
      checks.no_tool_called ??
      false,
  };
  const executionPassed = Object.values(executionChecks).every(Boolean);
  const toolChainPassed = Object.values(toolChecks).every(Boolean);
  return {
    scores: [
      booleanScore(
        "agent.runtime_trace_pass",
        executionPassed,
        "检查运行终态与 Trace 完整性",
        executionChecks,
      ),
      booleanScore(
        "agent.tool_mechanical_pass",
        toolChainPassed,
        "检查工具选中与机械执行链路",
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
  purpose: string,
  checks: Record<string, boolean>,
  metadata: Record<string, unknown> = {},
): any {
  const failedChecks = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => name);
  return {
    name,
    value,
    dataType: "BOOLEAN",
    comment: value
      ? `用途：${purpose}；结果：通过。`
      : `用途：${purpose}；失败：${failedChecks.join(", ")}。`,
    metadata: {
      ...metadata,
      checks,
    },
  };
}

function emptyArray(value: unknown): boolean {
  return Array.isArray(value) && value.length === 0;
}

function objectContains(
  actual: Record<string, unknown>,
  expected: Record<string, unknown>,
): boolean {
  return Object.entries(expected).every(
    ([key, value]) => JSON.stringify(actual?.[key]) === JSON.stringify(value),
  );
}

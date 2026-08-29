(function (root) {
  "use strict";

  const API_ORIGIN = "http://127.0.0.1:8089";
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const LEGACY_THREAD_ROUTE = /^\/studio\/thread\/([^/]+)\/?$/;
  const ORGANIZATION_THREAD_ROUTE = /^\/o\/([^/]+)\/studio\/thread\/?$/;
  const isRecord = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  const isUuid = (value) => typeof value === "string" && UUID.test(value);

  function parseStudioLocation(href) {
    try {
      const url = new URL(href);
      const legacy = url.pathname.match(LEGACY_THREAD_ROUTE);
      const organization = url.pathname.match(ORGANIZATION_THREAD_ROUTE);
      if (
        url.origin !== "https://smith.langchain.com" ||
        (!legacy && !organization) ||
        url.searchParams.get("baseUrl") !== API_ORIGIN
      ) {
        return null;
      }
      if (organization) {
        const organizationId = url.searchParams.get("organizationId");
        if (
          !isUuid(organization[1]) ||
          (organizationId && organizationId !== organization[1])
        ) {
          return null;
        }
      }
      const threadId = legacy ? legacy[1] : url.searchParams.get("threadId");
      return isUuid(threadId) ? { baseUrl: API_ORIGIN, threadId } : null;
    } catch {
      return null;
    }
  }

  function extractHitlSnapshot(state) {
    if (!isRecord(state) || !isRecord(state.checkpoint)) return null;
    const checkpointId = state.checkpoint.checkpoint_id;
    if (typeof checkpointId !== "string" || !checkpointId) return null;

    const unique = new Map();
    const collect = (interrupts) => {
      if (!Array.isArray(interrupts)) return;
      for (const item of interrupts) {
        if (isRecord(item) && typeof item.id === "string" && item.id) {
          unique.set(item.id, item);
        }
      }
    };
    collect(state.interrupts);
    if (Array.isArray(state.tasks)) {
      for (const task of state.tasks) collect(task && task.interrupts);
    }
    if (unique.size !== 1) return null;

    const nativeInterrupt = unique.values().next().value;
    const request = nativeInterrupt.value;
    if (
      !isRecord(request) ||
      !Array.isArray(request.action_requests) ||
      request.action_requests.length === 0 ||
      !Array.isArray(request.review_configs)
    ) {
      return null;
    }
    const validActions = request.action_requests.every(
      (action) =>
        isRecord(action) &&
        typeof action.name === "string" &&
        action.name &&
        isRecord(action.args),
    );
    if (!validActions) return null;

    return {
      identity: `${checkpointId}:${nativeInterrupt.id}`,
      checkpointId,
      interruptId: nativeInterrupt.id,
      request,
    };
  }

  function dereference(rootSchema, schema) {
    if (!isRecord(rootSchema) || !isRecord(schema)) return schema;
    if (typeof schema.$ref !== "string" || !schema.$ref.startsWith("#/$defs/")) {
      return schema;
    }
    return rootSchema.$defs && rootSchema.$defs[schema.$ref.slice(8)]
      ? rootSchema.$defs[schema.$ref.slice(8)]
      : schema;
  }

  function schemaAtPath(rootSchema, path) {
    if (!isRecord(rootSchema) || !Array.isArray(path)) return null;
    let schema = rootSchema;
    for (const part of path) {
      schema = dereference(rootSchema, schema);
      if (!isRecord(schema)) return null;
      schema =
        typeof part === "number"
          ? schema.items
          : schema.properties && schema.properties[part];
      if (!schema) return null;
    }
    return dereference(rootSchema, schema);
  }

  function fieldKind(value, schema) {
    if (value === null) return "null";
    const type = isRecord(schema) ? schema.type : null;
    if (type === "integer" || type === "number") return "number";
    if (["string", "boolean", "array", "object", "null"].includes(type)) {
      return type;
    }
    if (Array.isArray(value)) return "array";
    return typeof value === "object" ? "object" : typeof value;
  }

  function reviewConfigFor(request, index) {
    if (!isRecord(request) || !Array.isArray(request.action_requests)) return null;
    const action = request.action_requests[index];
    const configs = Array.isArray(request.review_configs)
      ? request.review_configs
      : [];
    return (
      configs.find((config) => config && config.action_name === action?.name) ||
      configs[index] ||
      null
    );
  }

  function buildDecision(action, type, editedArgs, reason) {
    if (type === "approve") return { type: "approve" };
    if (type === "edit") {
      if (!isRecord(action) || !isRecord(editedArgs)) {
        throw new TypeError("edit requires an action and edited args");
      }
      return {
        type: "edit",
        edited_action: { name: action.name, args: editedArgs },
      };
    }
    if (type === "reject") {
      const message = typeof reason === "string" ? reason.trim() : "";
      if (!message) throw new TypeError("reject requires a reason");
      return { type: "reject", message };
    }
    throw new TypeError(`unsupported decision: ${type}`);
  }

  function sameIdentity(left, right) {
    return Boolean(left && right && left.identity === right.identity);
  }

  root.StudioHitlCore = Object.freeze({
    API_ORIGIN,
    buildDecision,
    extractHitlSnapshot,
    fieldKind,
    isUuid,
    parseStudioLocation,
    reviewConfigFor,
    sameIdentity,
    schemaAtPath,
  });
})(globalThis);

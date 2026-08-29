importScripts("core.js");

(function (root) {
  "use strict";

  const core = root.StudioHitlCore;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  class BridgeError extends Error {
    constructor(code) {
      super(code);
      this.code = code;
    }
  }

  async function requestJson(path, options = {}, fetchImpl = fetch) {
    const response = await fetchImpl(`${core.API_ORIGIN}${path}`, {
      ...options,
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new BridgeError("server_error");
    return response.json();
  }

  function validateEnvelope(message, sender) {
    const location = core.parseStudioLocation(sender && sender.url);
    if (
      !location ||
      !message ||
      !UUID.test(message.threadId || "") ||
      location.threadId !== message.threadId
    ) {
      throw new BridgeError("forbidden");
    }
    return message.threadId;
  }

  function publicSnapshot(snapshot) {
    if (!snapshot) return null;
    return {
      identity: snapshot.identity,
      checkpointId: snapshot.checkpointId,
      interruptId: snapshot.interruptId,
      request: snapshot.request,
    };
  }

  function validDecisions(snapshot, decisions) {
    if (!Array.isArray(decisions) || decisions.length !== snapshot.request.action_requests.length) {
      return false;
    }
    return decisions.every((decision, index) => {
      if (!decision || typeof decision !== "object") return false;
      const action = snapshot.request.action_requests[index];
      const config = core.reviewConfigFor(snapshot.request, index);
      const allowed = config && Array.isArray(config.allowed_decisions)
        ? config.allowed_decisions
        : [];
      if (!allowed.includes(decision.type)) return false;
      if (decision.type === "approve") return true;
      if (decision.type === "reject") {
        return typeof decision.message === "string" && Boolean(decision.message.trim());
      }
      return Boolean(
        decision.type === "edit" &&
          decision.edited_action &&
          decision.edited_action.name === action.name &&
          decision.edited_action.args &&
          typeof decision.edited_action.args === "object" &&
          !Array.isArray(decision.edited_action.args),
      );
    });
  }

  async function handleMessage(message, sender, fetchImpl = fetch) {
    try {
      const threadId = validateEnvelope(message, sender);
      const threadPath = `/threads/${threadId}`;

      if (message.type === "studio_hitl.get") {
        const state = await requestJson(`${threadPath}/state`, {}, fetchImpl);
        return { ok: true, snapshot: publicSnapshot(core.extractHitlSnapshot(state)) };
      }

      if (message.type === "studio_hitl.run") {
        if (!UUID.test(message.runId || "")) throw new BridgeError("invalid_request");
        const run = await requestJson(
          `${threadPath}/runs/${message.runId}`,
          {},
          fetchImpl,
        );
        return { ok: true, status: run.status || null };
      }

      if (message.type !== "studio_hitl.resume") {
        throw new BridgeError("invalid_request");
      }

      const state = await requestJson(`${threadPath}/state`, {}, fetchImpl);
      const snapshot = core.extractHitlSnapshot(state);
      if (!snapshot || snapshot.identity !== message.expectedIdentity) {
        return { ok: false, code: "stale_interrupt" };
      }
      if (!validDecisions(snapshot, message.decisions)) {
        throw new BridgeError("invalid_request");
      }

      const listed = await requestJson(
        `${threadPath}/runs?status=interrupted&limit=10`,
        {},
        fetchImpl,
      );
      const runs = Array.isArray(listed) ? listed : listed.runs;
      const latest = Array.isArray(runs)
        ? runs
            .filter((run) => run && typeof run.assistant_id === "string")
            .sort((left, right) =>
              String(right.created_at || "").localeCompare(String(left.created_at || "")),
            )[0]
        : null;
      if (!latest) throw new BridgeError("interrupted_run_not_found");

      const run = await requestJson(
        `${threadPath}/runs`,
        {
          method: "POST",
          body: JSON.stringify({
            assistant_id: latest.assistant_id,
            command: { resume: { decisions: message.decisions } },
            multitask_strategy: "reject",
          }),
        },
        fetchImpl,
      );
      if (!run || !UUID.test(run.run_id || "")) throw new BridgeError("server_error");
      return { ok: true, runId: run.run_id };
    } catch (error) {
      return {
        ok: false,
        code: error instanceof BridgeError ? error.code : "network_error",
      };
    }
  }

  root.StudioHitlBackground = Object.freeze({ handleMessage });

  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      handleMessage(message, sender).then(sendResponse);
      return true;
    });
  }
})(globalThis);

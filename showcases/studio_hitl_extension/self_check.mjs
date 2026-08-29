import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const context = vm.createContext({ AbortSignal, URL, console, structuredClone });
context.globalThis = context;
vm.runInContext(
  readFileSync(new URL("./core.js", import.meta.url), "utf8"),
  context,
);
context.importScripts = () => {};
vm.runInContext(
  readFileSync(new URL("./background.js", import.meta.url), "utf8"),
  context,
);

const core = context.StudioHitlCore;
const bridge = context.StudioHitlBackground;
const plain = (value) => JSON.parse(JSON.stringify(value));
const threadId = "11111111-1111-4111-8111-111111111111";
const sender = {
  url: `https://smith.langchain.com/studio/thread/${threadId}?baseUrl=http%3A%2F%2F127.0.0.1%3A8089`,
};
const request = {
  action_requests: [
    {
      name: "execute",
      description: "执行测试命令",
      args: {
        command: "python -m pytest -q",
        timeout_seconds: 30,
        enabled: true,
        env: { MODE: "mock" },
        paths: ["tests/core"],
      },
    },
    {
      name: "write_file",
      args: { path: "notes.txt", content: "hello" },
    },
  ],
  review_configs: [
    {
      action_name: "execute",
      allowed_decisions: ["approve", "edit", "reject"],
      args_schema: {
        type: "object",
        properties: {
          command: {
            type: "string",
            enum: [
              "python -m pytest -q",
              "python -m compileall -q src",
            ],
          },
          timeout_seconds: { type: "integer" },
          enabled: { type: "boolean" },
          env: {
            type: "object",
            properties: { MODE: { type: "string" } },
          },
          paths: { type: "array", items: { type: "string" } },
        },
      },
    },
    {
      action_name: "write_file",
      allowed_decisions: ["approve", "edit", "reject"],
    },
  ],
};
const interrupt = { id: "interrupt-1", value: request };
const state = {
  checkpoint: { checkpoint_id: "checkpoint-1" },
  interrupts: [interrupt],
  tasks: [{ id: "task-1", name: "review", interrupts: [interrupt] }],
};

test("parses only the fixed Studio route", () => {
  assert.deepEqual(
    plain(
      core.parseStudioLocation(
        "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111?baseUrl=http%3A%2F%2F127.0.0.1%3A8089",
      ),
    ),
    {
      baseUrl: "http://127.0.0.1:8089",
      threadId: "11111111-1111-4111-8111-111111111111",
    },
  );
  assert.equal(
    core.parseStudioLocation(
      "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111?baseUrl=https%3A%2F%2Fexample.com",
    ),
    null,
  );
});

test("deduplicates the same interrupt exposed at two state locations", () => {
  const snapshot = core.extractHitlSnapshot(state);

  assert.equal(snapshot.identity, "checkpoint-1:interrupt-1");
  assert.equal(snapshot.request.action_requests.length, 2);
});

test("rejects multiple native interrupts", () => {
  const changed = structuredClone(state);
  changed.interrupts.push({ id: "interrupt-2", value: request });

  assert.equal(core.extractHitlSnapshot(changed), null);
});

test("preserves field kinds and decision wire keys", () => {
  assert.deepEqual(
    ["x", 1, true, [1], { a: 1 }, null].map((value) =>
      core.fieldKind(value),
    ),
    ["string", "number", "boolean", "array", "object", "null"],
  );
  assert.deepEqual(
    plain(core.buildDecision(request.action_requests[0], "approve")),
    { type: "approve" },
  );
  assert.deepEqual(
    plain(
      core.buildDecision(request.action_requests[0], "edit", {
        timeout_seconds: 45,
      }),
    ),
    {
      type: "edit",
      edited_action: {
        name: "execute",
        args: { timeout_seconds: 45 },
      },
    },
  );
  assert.deepEqual(
    plain(
      core.buildDecision(
        request.action_requests[0],
        "reject",
        undefined,
        "命令不安全",
      ),
    ),
    { type: "reject", message: "命令不安全" },
  );
});

test("prefers JSON schema and resolves nested property and item schemas", () => {
  const schema = request.review_configs[0].args_schema;

  assert.equal(
    core.fieldKind("30", core.schemaAtPath(schema, ["timeout_seconds"])),
    "number",
  );
  assert.equal(
    core.fieldKind("false", core.schemaAtPath(schema, ["enabled"])),
    "boolean",
  );
  assert.equal(core.schemaAtPath(schema, ["paths", 0]).type, "string");
  assert.deepEqual(plain(core.schemaAtPath(schema, ["command"]).enum), [
    "python -m pytest -q",
    "python -m compileall -q src",
  ]);
});

test("matches review config by action name and compares identity", () => {
  assert.equal(core.reviewConfigFor(request, 1).action_name, "write_file");
  assert.equal(
    core.sameIdentity({ identity: "a" }, { identity: "a" }),
    true,
  );
  assert.equal(
    core.sameIdentity({ identity: "a" }, { identity: "b" }),
    false,
  );
});

test("revalidates state and resumes the newest interrupted assistant", async () => {
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith(`/threads/${threadId}/state`)) {
      return jsonResponse(state);
    }
    if (url.includes(`/threads/${threadId}/runs?`)) {
      return jsonResponse([
        {
          run_id: "22222222-2222-4222-8222-222222222222",
          assistant_id: "assistant-old",
          created_at: "2026-08-29T01:00:00Z",
        },
        {
          run_id: "33333333-3333-4333-8333-333333333333",
          assistant_id: "assistant-new",
          created_at: "2026-08-29T02:00:00Z",
        },
      ]);
    }
    return jsonResponse({
      run_id: "44444444-4444-4444-8444-444444444444",
    });
  };
  const decisions = [
    { type: "approve" },
    { type: "reject", message: "不需要写文件" },
  ];

  const result = await bridge.handleMessage(
    {
      type: "studio_hitl.resume",
      threadId,
      expectedIdentity: "checkpoint-1:interrupt-1",
      decisions,
    },
    sender,
    fakeFetch,
  );

  assert.deepEqual(plain(result), {
    ok: true,
    runId: "44444444-4444-4444-8444-444444444444",
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[2].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    assistant_id: "assistant-new",
    command: { resume: { decisions } },
    multitask_strategy: "reject",
  });
});

test("fails closed when the interrupt identity is stale", async () => {
  const changed = structuredClone(state);
  changed.checkpoint.checkpoint_id = "checkpoint-2";
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    calls.push({ url, options });
    return jsonResponse(changed);
  };

  const result = await bridge.handleMessage(
    {
      type: "studio_hitl.resume",
      threadId,
      expectedIdentity: "checkpoint-1:interrupt-1",
      decisions: [{ type: "approve" }, { type: "approve" }],
    },
    sender,
    fakeFetch,
  );

  assert.deepEqual(plain(result), { ok: false, code: "stale_interrupt" });
  assert.equal(calls.length, 1);
});

test("keeps the MV3 extension narrow and avoids unsafe DOM sinks", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("./manifest.json", import.meta.url), "utf8"),
  );
  const content = readFileSync(
    new URL("./content.js", import.meta.url),
    "utf8",
  );

  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(plain(manifest.host_permissions), [
    "http://127.0.0.1:8089/*",
  ]);
  assert.deepEqual(plain(manifest.content_scripts), [
    {
      matches: ["https://smith.langchain.com/studio/*"],
      js: ["core.js", "content.js"],
      run_at: "document_idle",
    },
  ]);
  assert.deepEqual(plain(manifest.background), {
    service_worker: "background.js",
  });
  assert.equal(content.includes("innerHTML"), false);
  assert.equal(content.includes("eval("), false);
});

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => structuredClone(body),
  };
}

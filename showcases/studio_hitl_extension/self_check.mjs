import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const context = vm.createContext({ URL, console, structuredClone });
context.globalThis = context;
vm.runInContext(
  readFileSync(new URL("./core.js", import.meta.url), "utf8"),
  context,
);

const core = context.StudioHitlCore;
const plain = (value) => JSON.parse(JSON.stringify(value));
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

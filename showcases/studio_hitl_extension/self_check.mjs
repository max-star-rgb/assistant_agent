import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const context = vm.createContext({ AbortSignal, URL, console, structuredClone });
context.globalThis = context;
vm.runInContext(
  readFileSync(new URL("./extension/core.js", import.meta.url), "utf8"),
  context,
);
context.importScripts = () => {};
vm.runInContext(
  readFileSync(new URL("./extension/background.js", import.meta.url), "utf8"),
  context,
);

const core = context.StudioHitlCore;
const bridge = context.StudioHitlBackground;
const plain = (value) => JSON.parse(JSON.stringify(value));
const organizationId = "53510e38-f838-45fb-86ac-84fa3bf258a3";
const threadId = "01a04c9f-ea6b-7991-a3ec-e064b9be7ade";
const sender = {
  url: `https://smith.langchain.com/o/${organizationId}/studio/thread?organizationId=${organizationId}&render=interact&baseUrl=http%3A%2F%2F127.0.0.1%3A8089&mode=chat&assistantId=8d030b92-89be-5d58-918d-ff35e996429a&threadId=${threadId}`,
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

test("parses current and legacy Studio thread routes", () => {
  assert.deepEqual(plain(core.parseStudioLocation(sender.url)), {
    baseUrl: "http://127.0.0.1:8089",
    threadId,
  });
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
    readFileSync(new URL("./extension/manifest.json", import.meta.url), "utf8"),
  );
  const content = readFileSync(
    new URL("./extension/content.js", import.meta.url),
    "utf8",
  );

  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(plain(manifest.host_permissions), [
    "http://127.0.0.1:8089/*",
  ]);
  assert.deepEqual(plain(manifest.content_scripts), [
    {
      matches: [
        "https://smith.langchain.com/studio/*",
        "https://smith.langchain.com/o/*/studio/*",
      ],
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

test("keeps the loadable Chrome bundle isolated from caches and source files", () => {
  assert.deepEqual(
    readdirSync(new URL("./extension/", import.meta.url)).sort(),
    ["background.js", "content.js", "core.js", "manifest.json"],
  );
});

test("shows args and requires an explicit decision, then recovers after failure", async () => {
  const harness = await contentHarness({ ok: false, code: "network_error" });

  assert.ok(harness.find((node) => node.textContent === "python -m pytest -q"));
  assert.equal(harness.button("提交全部决定").disabled, true);

  harness.button("批准").fire("click");
  assert.equal(harness.button("提交全部决定").disabled, false);
  harness.button("提交全部决定").fire("click");
  await settle();

  assert.equal(
    harness.sent.filter((message) => message.type === "studio_hitl.resume").length,
    1,
  );
  assert.equal(harness.button("提交全部决定").disabled, false);
  assert.match(harness.find((node) => node.attributes.role === "alert").textContent, /network_error/);
});

test("blocks invalid edits and lets the user fall back to Studio", async () => {
  const harness = await contentHarness({ ok: true, runId: "unused" });

  harness.button("编辑参数").fire("click");
  const numberInput = harness.find((node) => node.type === "number");
  numberInput.value = "";
  numberInput.fire("change");
  harness.button("提交全部决定").fire("click");
  await settle();

  assert.equal(
    harness.sent.filter((message) => message.type === "studio_hitl.resume").length,
    0,
  );
  assert.match(harness.find((node) => node.attributes.role === "alert").textContent, /有效/);

  harness.button("使用 Studio 原界面").fire("click");
  assert.equal(harness.document.documentElement.children.length, 0);
  await harness.pollWith({ ok: false, code: "network_error" });
  assert.equal(harness.document.documentElement.children.length, 0);
});

async function contentHarness(resumeResponse) {
  class FakeNode {
    constructor(tag) {
      this.tag = tag;
      this.children = [];
      this.listeners = {};
      this.attributes = {};
      this.className = "";
      this.textContent = "";
      this.value = "";
      this.disabled = false;
      this.checked = false;
      this.classList = {
        add: (name) => {
          this.className = `${this.className} ${name}`.trim();
        },
      };
    }

    append(...nodes) {
      for (const node of nodes) {
        node.parent = this;
        this.children.push(node);
      }
    }

    attachShadow() {
      this.shadowRoot = new FakeNode("shadow");
      return this.shadowRoot;
    }

    addEventListener(type, listener) {
      this.listeners[type] = listener;
    }

    fire(type) {
      return this.listeners[type] && this.listeners[type]();
    }

    setAttribute(name, value) {
      this.attributes[name] = value;
    }

    setCustomValidity(message) {
      this.validationMessage = message;
    }

    querySelectorAll(selector) {
      const classes = selector.split(",").map((item) => item.slice(1));
      return walk(this).filter((node) =>
        classes.some((name) => node.className.split(" ").includes(name)),
      );
    }

    remove() {
      if (this.parent) {
        this.parent.children = this.parent.children.filter((node) => node !== this);
        this.parent = null;
      }
    }
  }

  const documentElement = new FakeNode("html");
  const fakeDocument = {
    createElement: (tag) => new FakeNode(tag),
    documentElement,
    hidden: false,
  };
  const sent = [];
  const timers = [];
  const uiSnapshot = plain(core.extractHitlSnapshot(state));
  uiSnapshot.request.action_requests = [uiSnapshot.request.action_requests[0]];
  uiSnapshot.request.review_configs = [uiSnapshot.request.review_configs[0]];
  let getResponse = { ok: true, snapshot: uiSnapshot };
  const uiContext = vm.createContext({
    URL,
    chrome: {
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          sent.push(plain(message));
          callback(
            message.type === "studio_hitl.get"
              ? getResponse
              : resumeResponse,
          );
        },
      },
    },
    console,
    document: fakeDocument,
    location: { href: sender.url, reload() {} },
    setTimeout(callback) {
      timers.push(callback);
    },
    structuredClone,
  });
  uiContext.globalThis = uiContext;
  vm.runInContext(
    readFileSync(new URL("./extension/core.js", import.meta.url), "utf8"),
    uiContext,
  );
  vm.runInContext(
    readFileSync(new URL("./extension/content.js", import.meta.url), "utf8"),
    uiContext,
  );
  await settle();

  const all = () => walk(documentElement);
  return {
    button: (text) => all().find((node) => node.tag === "button" && node.textContent === text),
    document: fakeDocument,
    find: (predicate) => all().find(predicate),
    async pollWith(response) {
      getResponse = response;
      timers.shift()();
      await settle();
    },
    sent,
  };
}

function walk(root) {
  const result = [];
  const visit = (node) => {
    result.push(node);
    for (const child of node.children || []) visit(child);
    if (node.shadowRoot) visit(node.shadowRoot);
  };
  visit(root);
  return result;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => structuredClone(body),
  };
}

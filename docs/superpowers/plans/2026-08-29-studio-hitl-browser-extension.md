# Studio HITL Chrome Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 LangSmith Studio thread 页面内自动显示可查看、可按字段编辑、可批准或拒绝的本地 HITL 审批浮层，并恢复同一个 Agent Server thread。

**Architecture:** 使用无构建链的 Chrome Manifest V3 扩展。content script 只负责 Shadow DOM UI，background service worker 只访问固定 `127.0.0.1:8089` 公开 API，经典脚本 `core.js` 提供两者共享且可被 Node 直接验证的纯逻辑；独立离线 Graph 确定性产生标准 HITL payload。

**Tech Stack:** Chrome Manifest V3、原生 JavaScript/DOM/Shadow DOM、Node 18 标准库、Python 3.12、LangGraph 1.2、pytest。

**Spec:** `docs/superpowers/specs/2026-08-29-studio-hitl-browser-extension-design.md`

## Global Constraints

- 只支持 Google Chrome、`https://smith.langchain.com/studio/*` 和精确的 `http://127.0.0.1:8089`。
- 不增加 npm package、React、bundler、前端服务、自定义 resume endpoint 或另一套 run manager。
- 保持标准 `action_requests`、`review_configs`、`Command(resume={"decisions": [...]})` 协议。
- 第一版只接管恰好一个 native interrupt；它可以包含多个 action。
- 未知 payload、多个 native interrupt、stale identity、网络错误和服务端错误全部 fail closed。
- resume 请求超时 10 秒且不自动重试；提交期间禁止重复点击。
- 不使用 `innerHTML`、`eval` 或动态代码执行，不请求和保存 API Key、Provider 响应；state 响应只投影 checkpoint 与 interrupt，不解析、传递或保存 messages。
- 所有自动验证使用 mock/local/offline；不调用真实 Provider。
- 不修改 `tests/core` 或 core invariant；临时 Python RED/GREEN 只放 `tests/tdd/studio-hitl-extension/`。
- 当前工作区有大量用户改动；每次只暂存和提交本任务明确列出的文件。

## File Map

| 文件 | 职责 |
| --- | --- |
| `showcases/studio_hitl_extension/manifest.json` | 固定 Chrome host/content/background 权限 |
| `showcases/studio_hitl_extension/core.js` | HITL 解析、identity、URL、字段类型和 decision 纯逻辑 |
| `showcases/studio_hitl_extension/background.js` | 固定 Agent Server API bridge、stale 复核与 resume |
| `showcases/studio_hitl_extension/content.js` | SPA 路由观察、轮询、Shadow DOM 表单和状态条 |
| `showcases/studio_hitl_extension/self_check.mjs` | 无依赖 Node RED/GREEN 检查 |
| `showcases/studio_hitl_extension/graph.py` | 无模型、无 Tool 的确定性 HITL showcase Graph |
| `showcases/studio_hitl_extension/README.md` | 安装、启动、验收和卸载说明 |
| `langgraph.showcase.json` | 注册 `studio-hitl-showcase` |
| `tests/tdd/studio-hitl-extension/test_graph.py` | showcase Graph 临时 RED/GREEN |
| `tests/tdd/studio-evolution-showcase/test_graph.py` | 更新 showcase config 精确 graph 清单 |

---

### Task 1: 实现共享 HITL 纯逻辑

**Files:**
- Create: `showcases/studio_hitl_extension/self_check.mjs`
- Create: `showcases/studio_hitl_extension/core.js`

**Interfaces:**
- Consumes: Agent Server `ThreadState` 的 `checkpoint`、`interrupts`、`tasks[].interrupts`。
- Produces: `globalThis.StudioHitlCore`，包含 `parseStudioLocation(href)`、`extractHitlSnapshot(state)`、`fieldKind(value, schema)`、`schemaAtPath(rootSchema, path)`、`reviewConfigFor(request, index)`、`buildDecision(action, type, editedArgs, reason)` 和 `sameIdentity(left, right)`。

- [ ] **Step 1: 先写纯逻辑失败检查**

创建 `self_check.mjs`，使用 Node 标准库加载经典脚本并覆盖以下结构化行为：

```js
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
    { name: "write_file", args: { path: "notes.txt", content: "hello" } },
  ],
  review_configs: [
    {
      action_name: "execute",
      allowed_decisions: ["approve", "edit", "reject"],
      args_schema: {
        type: "object",
        properties: {
          command: { type: "string", enum: ["python -m pytest -q", "python -m compileall -q src"] },
          timeout_seconds: { type: "integer" },
          enabled: { type: "boolean" },
          env: { type: "object", properties: { MODE: { type: "string" } } },
          paths: { type: "array", items: { type: "string" } },
        },
      },
    },
    { action_name: "write_file", allowed_decisions: ["approve", "edit", "reject"] },
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
    plain(core.parseStudioLocation(
      "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111?baseUrl=http%3A%2F%2F127.0.0.1%3A8089",
    )),
    {
      baseUrl: "http://127.0.0.1:8089",
      threadId: "11111111-1111-4111-8111-111111111111",
    },
  );
  assert.equal(core.parseStudioLocation(
    "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111?baseUrl=https%3A%2F%2Fexample.com",
  ), null);
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
    ["x", 1, true, [1], { a: 1 }, null].map(core.fieldKind),
    ["string", "number", "boolean", "array", "object", "null"],
  );
  assert.deepEqual(plain(core.buildDecision(request.action_requests[0], "approve")), { type: "approve" });
  assert.deepEqual(
    plain(core.buildDecision(request.action_requests[0], "edit", { timeout_seconds: 45 })),
    { type: "edit", edited_action: { name: "execute", args: { timeout_seconds: 45 } } },
  );
  assert.deepEqual(
    plain(core.buildDecision(request.action_requests[0], "reject", undefined, "命令不安全")),
    { type: "reject", message: "命令不安全" },
  );
});

test("prefers JSON schema and resolves nested property and item schemas", () => {
  const schema = request.review_configs[0].args_schema;
  assert.equal(core.fieldKind("30", core.schemaAtPath(schema, ["timeout_seconds"])), "number");
  assert.equal(core.fieldKind("false", core.schemaAtPath(schema, ["enabled"])), "boolean");
  assert.equal(core.schemaAtPath(schema, ["paths", 0]).type, "string");
  assert.deepEqual(
    plain(core.schemaAtPath(schema, ["command"]).enum),
    ["python -m pytest -q", "python -m compileall -q src"],
  );
});

test("matches review config by action name and compares identity", () => {
  assert.equal(core.reviewConfigFor(request, 1).action_name, "write_file");
  assert.equal(core.sameIdentity({ identity: "a" }, { identity: "a" }), true);
  assert.equal(core.sameIdentity({ identity: "a" }, { identity: "b" }), false);
});
```

- [ ] **Step 2: 运行检查确认 RED**

Run:

```bash
node --test showcases/studio_hitl_extension/self_check.mjs
```

Expected: FAIL，错误为找不到 `core.js`。

- [ ] **Step 3: 实现最小经典脚本 API**

创建 `core.js`。保持纯函数，不读取 DOM、Chrome API 或网络：

```js
(() => {
  "use strict";

  const API_ORIGIN = "http://127.0.0.1:8089";
  const THREAD_PATH = /^\/studio\/thread\/([0-9a-f-]{36})(?:\/|$)/i;
  const SUPPORTED_DECISIONS = new Set(["approve", "edit", "reject"]);
  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

  function parseStudioLocation(href) {
    const url = new URL(href);
    const match = url.pathname.match(THREAD_PATH);
    if (!match || url.searchParams.get("baseUrl") !== API_ORIGIN) return null;
    return { baseUrl: API_ORIGIN, threadId: match[1] };
  }

  function extractHitlSnapshot(state) {
    if (!isObject(state)) return null;
    const byId = new Map();
    for (const item of Array.isArray(state.interrupts) ? state.interrupts : []) {
      if (typeof item?.id === "string") byId.set(item.id, item);
    }
    for (const task of Array.isArray(state.tasks) ? state.tasks : []) {
      for (const item of Array.isArray(task?.interrupts) ? task.interrupts : []) {
        if (typeof item?.id === "string") byId.set(item.id, item);
      }
    }
    if (byId.size !== 1) return null;
    const [interrupt] = byId.values();
    const checkpointId = state.checkpoint?.checkpoint_id;
    const request = interrupt.value;
    if (
      typeof checkpointId !== "string" ||
      !isObject(request) ||
      !Array.isArray(request.action_requests) ||
      request.action_requests.length === 0 ||
      !Array.isArray(request.review_configs) ||
      request.review_configs.length === 0 ||
      request.action_requests.some((action) =>
        !isObject(action) || typeof action.name !== "string" || !isObject(action.args)
      )
    ) return null;
    return {
      identity: `${checkpointId}:${interrupt.id}`,
      checkpointId,
      interruptId: interrupt.id,
      request,
    };
  }

  function dereference(root, schema) {
    if (!schema?.$ref?.startsWith("#/$defs/")) return schema;
    return root?.$defs?.[schema.$ref.slice("#/$defs/".length)] ?? schema;
  }

  function schemaAtPath(root, path) {
    let schema = dereference(root, root);
    for (const part of path) {
      schema = typeof part === "number"
        ? dereference(root, schema?.items)
        : dereference(root, schema?.properties?.[part]);
      if (!schema) return null;
    }
    return schema ?? null;
  }

  function fieldKind(value, schema = null) {
    if (value === null) return "null";
    const schemaType = Array.isArray(schema?.type)
      ? schema.type.find((item) => item !== "null")
      : schema?.type;
    if (schemaType === "integer" || schemaType === "number") return "number";
    if (["string", "boolean", "array", "object", "null"].includes(schemaType)) return schemaType;
    if (Array.isArray(value)) return "array";
    if (isObject(value)) return "object";
    if (["string", "number", "boolean"].includes(typeof value)) return typeof value;
    throw new TypeError("Unsupported field value");
  }

  function reviewConfigFor(request, index) {
    const action = request.action_requests[index];
    return request.review_configs.find((item) => item.action_name === action.name)
      ?? request.review_configs[index]
      ?? null;
  }

  function buildDecision(action, type, editedArgs, reason) {
    if (!SUPPORTED_DECISIONS.has(type)) throw new TypeError("Unsupported decision");
    if (type === "approve") return { type };
    if (type === "edit") {
      if (!isObject(editedArgs)) throw new TypeError("Edited args must be an object");
      return { type, edited_action: { name: action.name, args: editedArgs } };
    }
    const message = String(reason ?? "").trim();
    if (!message) throw new TypeError("Rejection reason is required");
    return { type, message };
  }

  const sameIdentity = (left, right) => Boolean(left?.identity && left.identity === right?.identity);

  globalThis.StudioHitlCore = Object.freeze({
    API_ORIGIN,
    buildDecision,
    extractHitlSnapshot,
    fieldKind,
    parseStudioLocation,
    reviewConfigFor,
    schemaAtPath,
    sameIdentity,
  });
})();
```

- [ ] **Step 4: 运行检查确认 GREEN**

Run: `node --test showcases/studio_hitl_extension/self_check.mjs`

Expected: 6 tests PASS。

- [ ] **Step 5: 只提交共享逻辑**

```bash
git add showcases/studio_hitl_extension/core.js showcases/studio_hitl_extension/self_check.mjs
git commit -m "feat: parse Studio HITL interrupts"
```

---

### Task 2: 实现固定 Agent Server API bridge

**Files:**
- Modify: `showcases/studio_hitl_extension/self_check.mjs`
- Create: `showcases/studio_hitl_extension/background.js`

**Interfaces:**
- Consumes: `StudioHitlCore.extractHitlSnapshot`、固定 API origin、Chrome `runtime.onMessage`。
- Produces: 三个消息：`studio_hitl.get`、`studio_hitl.resume`、`studio_hitl.run`；所有响应为 `{ok:true,...}` 或 `{ok:false,error:{code,message}}`。

- [ ] **Step 1: 增加 bridge 失败检查**

在 `self_check.mjs` 加载 `background.js`，提供只记录请求的 fake fetch；断言 get 只返回 HITL 投影、resume 会重新读取 state、按时间选择最新 interrupted run，并发送原生 command：

```js
context.importScripts = () => {};
context.AbortSignal = AbortSignal;
context.fetch = fetch;
vm.runInContext(
  readFileSync(new URL("./background.js", import.meta.url), "utf8"),
  context,
);
const bridge = context.StudioHitlBackground;

test("bridge rechecks identity and posts one native resume", async () => {
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/state")) return new Response(JSON.stringify(state), { status: 200 });
    if (url.includes("/runs?status=interrupted")) {
      return new Response(JSON.stringify([
        { run_id: "run-old", assistant_id: "assistant-old", created_at: "2026-01-01T00:00:00Z", status: "interrupted" },
        { run_id: "run-new", assistant_id: "assistant-new", created_at: "2026-01-02T00:00:00Z", status: "interrupted" },
      ]), { status: 200 });
    }
    return new Response(JSON.stringify({ run_id: "run-resumed", status: "pending" }), { status: 200 });
  };
  const response = await bridge.handleMessage(
    {
      type: "studio_hitl.resume",
      threadId: "11111111-1111-4111-8111-111111111111",
      expectedIdentity: "checkpoint-1:interrupt-1",
      decisions: [{ type: "approve" }, { type: "reject", message: "不写文件" }],
    },
    "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111",
    fakeFetch,
  );
  assert.equal(response.ok, true);
  const posted = JSON.parse(calls.at(-1).options.body);
  assert.deepEqual(posted, {
    assistant_id: "assistant-new",
    command: { resume: { decisions: [{ type: "approve" }, { type: "reject", message: "不写文件" }] } },
    multitask_strategy: "reject",
  });
});

test("bridge fails closed on stale identity without POST", async () => {
  const stale = structuredClone(state);
  stale.checkpoint.checkpoint_id = "checkpoint-2";
  let calls = 0;
  const fakeFetch = async () => {
    calls += 1;
    return new Response(JSON.stringify(stale), { status: 200 });
  };
  const response = await bridge.handleMessage(
    {
      type: "studio_hitl.resume",
      threadId: "11111111-1111-4111-8111-111111111111",
      expectedIdentity: "checkpoint-1:interrupt-1",
      decisions: [{ type: "approve" }],
    },
    "https://smith.langchain.com/studio/thread/11111111-1111-4111-8111-111111111111",
    fakeFetch,
  );
  assert.equal(response.ok, false);
  assert.equal(response.error.code, "stale_interrupt");
  assert.equal(calls, 1);
});
```

- [ ] **Step 2: 运行检查确认 RED**

Run: `node --test showcases/studio_hitl_extension/self_check.mjs`

Expected: FAIL，错误为找不到 `background.js`。

- [ ] **Step 3: 实现 bridge 与 Chrome listener**

`background.js` 使用经典 service worker，以便 `importScripts("core.js")`；固定 sender、UUID、路径和超时：

```js
importScripts("core.js");

(() => {
  "use strict";
  const core = globalThis.StudioHitlCore;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const STUDIO = "https://smith.langchain.com/studio/";

  class BridgeError extends Error {
    constructor(code, message) {
      super(message);
      this.code = code;
    }
  }

  async function requestJson(path, options = {}, fetchImpl = fetch) {
    const response = await fetchImpl(`${core.API_ORIGIN}${path}`, {
      ...options,
      signal: AbortSignal.timeout(10_000),
      headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
    });
    if (!response.ok) throw new BridgeError(`agent_server_${response.status}`, `Agent Server returned ${response.status}`);
    return response.json();
  }

  function validate(message, senderUrl) {
    if (!senderUrl?.startsWith(STUDIO)) throw new BridgeError("invalid_sender", "Request did not come from Studio");
    if (!UUID.test(message?.threadId ?? "")) throw new BridgeError("invalid_thread", "Invalid thread ID");
  }

  async function handleMessage(message, senderUrl, fetchImpl = fetch) {
    try {
      validate(message, senderUrl);
      const thread = encodeURIComponent(message.threadId);
      if (message.type === "studio_hitl.get") {
        const state = await requestJson(`/threads/${thread}/state`, {}, fetchImpl);
        return { ok: true, snapshot: core.extractHitlSnapshot(state) };
      }
      if (message.type === "studio_hitl.run") {
        if (!UUID.test(message.runId ?? "")) throw new BridgeError("invalid_run", "Invalid run ID");
        const run = await requestJson(`/threads/${thread}/runs/${encodeURIComponent(message.runId)}`, {}, fetchImpl);
        return { ok: true, run: { runId: run.run_id, status: run.status } };
      }
      if (message.type !== "studio_hitl.resume") throw new BridgeError("unsupported_message", "Unsupported extension request");

      const state = await requestJson(`/threads/${thread}/state`, {}, fetchImpl);
      const snapshot = core.extractHitlSnapshot(state);
      if (!snapshot || snapshot.identity !== message.expectedIdentity) {
        throw new BridgeError("stale_interrupt", "The pending interrupt changed");
      }
      if (!Array.isArray(message.decisions) || message.decisions.length !== snapshot.request.action_requests.length) {
        throw new BridgeError("invalid_decisions", "Every action needs one decision");
      }
      const runs = await requestJson(`/threads/${thread}/runs?status=interrupted&limit=10`, {}, fetchImpl);
      const latest = [...runs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      if (!latest?.assistant_id) throw new BridgeError("missing_run", "No interrupted run was found");
      const run = await requestJson(`/threads/${thread}/runs`, {
        method: "POST",
        body: JSON.stringify({
          assistant_id: latest.assistant_id,
          command: { resume: { decisions: message.decisions } },
          multitask_strategy: "reject",
        }),
      }, fetchImpl);
      return { ok: true, run: { runId: run.run_id, status: run.status } };
    } catch (error) {
      return {
        ok: false,
        error: {
          code: error instanceof BridgeError ? error.code : "network_error",
          message: error instanceof Error ? error.message : "Agent Server request failed",
        },
      };
    }
  }

  globalThis.StudioHitlBackground = Object.freeze({ handleMessage });
  globalThis.chrome?.runtime?.onMessage.addListener((message, sender, sendResponse) => {
    handleMessage(message, sender.url).then(sendResponse);
    return true;
  });
})();
```

- [ ] **Step 4: 运行检查确认 GREEN**

Run: `node --test showcases/studio_hitl_extension/self_check.mjs`

Expected: 8 tests PASS，fake fetch 只收到一次 POST。

- [ ] **Step 5: 提交 bridge**

```bash
git add showcases/studio_hitl_extension/background.js showcases/studio_hitl_extension/self_check.mjs
git commit -m "feat: resume Studio HITL through Agent Server"
```

---

### Task 3: 注入 Shadow DOM 审批表单

**Files:**
- Modify: `showcases/studio_hitl_extension/self_check.mjs`
- Create: `showcases/studio_hitl_extension/manifest.json`
- Create: `showcases/studio_hitl_extension/content.js`

**Interfaces:**
- Consumes: `StudioHitlCore`、Task 2 的三类 Chrome message。
- Produces: Studio 当前页面中的 `#assistant-agent-hitl-host` Shadow DOM；不导出产品 API。

- [ ] **Step 1: 增加 manifest 与安全静态 RED 检查**

在 `self_check.mjs` 增加：

```js
test("manifest grants only the approved hosts and scripts", () => {
  const manifest = JSON.parse(readFileSync(new URL("./manifest.json", import.meta.url), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1:8089/*"]);
  assert.deepEqual(manifest.content_scripts[0].matches, ["https://smith.langchain.com/studio/*"]);
  assert.deepEqual(manifest.content_scripts[0].js, ["core.js", "content.js"]);
  assert.equal(manifest.background.service_worker, "background.js");
  const content = readFileSync(new URL("./content.js", import.meta.url), "utf8");
  assert.equal(content.includes("innerHTML"), false);
  assert.equal(content.includes("eval("), false);
});
```

- [ ] **Step 2: 运行检查确认 RED**

Run: `node --test showcases/studio_hitl_extension/self_check.mjs`

Expected: FAIL，错误为找不到 `manifest.json`。

- [ ] **Step 3: 创建最小 Manifest V3**

```json
{
  "manifest_version": 3,
  "name": "Assistant Agent Studio HITL",
  "version": "0.1.0",
  "description": "Render local LangGraph HITL interrupts as forms inside Studio.",
  "host_permissions": ["http://127.0.0.1:8089/*"],
  "background": { "service_worker": "background.js" },
  "content_scripts": [
    {
      "matches": ["https://smith.langchain.com/studio/*"],
      "js": ["core.js", "content.js"],
      "run_at": "document_idle"
    }
  ]
}
```

- [ ] **Step 4: 实现 content script 状态机与安全 DOM helpers**

`content.js` 使用单个递归 `setTimeout`，每轮重新解析 SPA URL；可见时 1 秒、隐藏时 5 秒、离开 thread 时 1 秒后只重查 URL。所有节点使用 `createElement` 和 `textContent`：

```js
(() => {
  "use strict";
  const core = globalThis.StudioHitlCore;
  let snapshot = null;
  let drafts = [];
  let activeRunId = null;
  let host = null;
  let shadow = null;

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const send = (message) => new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
      else resolve(response);
    });
  });

  function ensureShadow() {
    if (shadow) return shadow;
    host = element("div");
    host.id = "assistant-agent-hitl-host";
    document.documentElement.append(host);
    shadow = host.attachShadow({ mode: "open" });
    const style = element("style");
    style.textContent = `
      :host{all:initial} .backdrop{position:fixed;inset:0;z-index:2147483647;background:#0f172a99;display:grid;place-items:center;padding:24px;font:14px system-ui,sans-serif}
      .panel{width:min(760px,100%);max-height:calc(100vh - 48px);overflow:auto;background:#fff;color:#111827;border-radius:16px;box-shadow:0 24px 80px #0006;padding:24px}
      .action{border:1px solid #d1d5db;border-radius:12px;padding:16px;margin:12px 0}.field{display:grid;gap:6px;margin:10px 0}.nested{border-left:3px solid #e5e7eb;padding-left:12px}
      label{font-weight:600} input,textarea,select{box-sizing:border-box;width:100%;border:1px solid #9ca3af;border-radius:8px;padding:8px;font:inherit} input[type=checkbox]{width:auto}
      .choices,.footer{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}button{border:1px solid #9ca3af;border-radius:8px;background:#fff;padding:8px 12px;cursor:pointer}button.primary{background:#2563eb;color:#fff;border-color:#2563eb}button:disabled{opacity:.5;cursor:not-allowed}.error{color:#b91c1c}.changed{outline:2px solid #f59e0b}.status{position:fixed;right:24px;bottom:24px;z-index:2147483647;background:#111827;color:#fff;border-radius:10px;padding:12px 16px;font:14px system-ui,sans-serif}
    `;
    shadow.append(style);
    return shadow;
  }
```

字段递归器必须直接更新 `drafts[actionIndex].args` 中对应路径，并保持原类型：

```js
  function setPath(root, path, value) {
    let cursor = root;
    for (const part of path.slice(0, -1)) cursor = cursor[part];
    cursor[path.at(-1)] = value;
  }

  function renderField(container, actionIndex, label, value, path, rootSchema) {
    const schema = core.schemaAtPath(rootSchema, path);
    const kind = core.fieldKind(value, schema);
    const row = element("div", kind === "array" || kind === "object" ? "field nested" : "field");
    row.append(element("label", null, String(label)));
    if (kind === "array" || kind === "object") {
      Object.entries(value).forEach(([key, child]) => renderField(
        row,
        actionIndex,
        key,
        child,
        [...path, Array.isArray(value) ? Number(key) : key],
        rootSchema,
      ));
    } else {
      const input = Array.isArray(schema?.enum)
        ? element("select")
        : kind === "string" && (value.includes("\n") || value.length > 120)
          ? element("textarea")
          : element("input");
      if (Array.isArray(schema?.enum)) {
        for (const optionValue of schema.enum) {
          const option = element("option", null, String(optionValue));
          option.value = JSON.stringify(optionValue);
          option.selected = Object.is(optionValue, value);
          input.append(option);
        }
      } else if (kind === "boolean") {
        input.type = "checkbox";
        input.checked = value;
      } else if (kind === "number") {
        input.type = "number";
        input.step = "any";
        input.value = String(value);
      } else {
        input.value = kind === "null" ? "null" : value;
      }
      input.addEventListener("change", () => {
        let next;
        if (Array.isArray(schema?.enum)) next = JSON.parse(input.value);
        else if (kind === "boolean") next = input.checked;
        else if (kind === "number") {
          next = Number(input.value);
          if (!Number.isFinite(next)) return input.setCustomValidity("请输入有效数字");
        } else if (kind === "null") {
          try { next = JSON.parse(input.value); } catch { return input.setCustomValidity("请输入有效 JSON 值"); }
        } else next = input.value;
        input.setCustomValidity("");
        setPath(drafts[actionIndex].args, path, next);
        input.classList.add("changed");
      });
      row.append(input);
    }
    container.append(row);
  }
```

同一文件剩余状态机按以下确定规则实现，不增加第二套类层次：

- `render(snapshot)` 为每个 action 初始化 `{type:"approve", args:structuredClone(action.args), reason:""}`；只有 identity 改变时重置。
- `reviewConfigFor` 决定三个 choice button 是否出现；切换到 edit 时显示递归表单，切换到 reject 时显示必填 textarea。
- “提交全部决定”依次调用 `buildDecision`；任一 action 未选择允许的 decision 或拒绝原因为空时只显示 error。
- `submit()` 调用 `studio_hitl.resume`，传 `expectedIdentity` 与有序 decisions；成功后保存 `activeRunId`，把 modal 替换成非阻塞 status。
- `poll()` 在 `activeRunId` 存在时调用 `studio_hitl.run`；状态进入 `success|error|timeout|interrupted` 后执行一次 `location.reload()`。
- 无 active run 时调用 `studio_hitl.get`；snapshot 为 null 时移除 host；错误只在已有浮层中显示，不伪造审批。
- `setTimeout(poll, document.hidden ? 5000 : 1000)` 放在 `finally`，保证没有重叠请求；`visibilitychange` 不创建第二个 timer。

使用下面的单状态机函数实现这些规则；`paint()` 每次只重建 Shadow DOM 中的交互节点，保留最前面的
`style` 节点：

```js
  let submitting = false;
  let errorText = "";

  function removeUi() {
    host?.remove();
    host = null;
    shadow = null;
  }

  function clearViews() {
    if (!shadow) return;
    shadow.querySelectorAll(".backdrop,.status").forEach((node) => node.remove());
  }

  function showStatus(text) {
    const root = ensureShadow();
    clearViews();
    root.append(element("div", "status", text));
  }

  function choose(actionIndex, type) {
    drafts[actionIndex].type = type;
    errorText = "";
    paint();
  }

  function paintAction(panel, action, actionIndex) {
    const config = core.reviewConfigFor(snapshot.request, actionIndex);
    const allowed = (config?.allowed_decisions ?? [])
      .filter((item) => ["approve", "edit", "reject"].includes(item));
    const card = element("section", "action");
    card.append(element("h2", null, action.name));
    if (action.description) card.append(element("p", null, action.description));
    const choices = element("div", "choices");
    for (const type of allowed) {
      const labels = { approve: "批准", edit: "编辑参数", reject: "拒绝" };
      const button = element("button", drafts[actionIndex].type === type ? "primary" : "", labels[type]);
      button.type = "button";
      button.disabled = submitting;
      button.addEventListener("click", () => choose(actionIndex, type));
      choices.append(button);
    }
    card.append(choices);
    if (drafts[actionIndex].type === "edit") {
      Object.entries(drafts[actionIndex].args)
        .forEach(([key, value]) => renderField(
          card,
          actionIndex,
          key,
          value,
          [key],
          config?.args_schema ?? null,
        ));
    }
    if (drafts[actionIndex].type === "reject") {
      const row = element("div", "field");
      row.append(element("label", null, "拒绝原因"));
      const textarea = element("textarea");
      textarea.required = true;
      textarea.value = drafts[actionIndex].reason;
      textarea.disabled = submitting;
      textarea.addEventListener("input", () => { drafts[actionIndex].reason = textarea.value; });
      row.append(textarea);
      card.append(row);
    }
    panel.append(card);
  }

  function paint() {
    if (!snapshot) return removeUi();
    const root = ensureShadow();
    clearViews();
    const backdrop = element("div", "backdrop");
    const panel = element("main", "panel");
    panel.append(element("h1", null, "需要你的批准"));
    snapshot.request.action_requests
      .forEach((action, index) => paintAction(panel, action, index));
    if (errorText) panel.append(element("p", "error", errorText));
    const footer = element("div", "footer");
    const submitButton = element("button", "primary", submitting ? "正在提交…" : "提交全部决定");
    submitButton.type = "button";
    submitButton.disabled = submitting;
    submitButton.addEventListener("click", submit);
    footer.append(submitButton);
    panel.append(footer);
    backdrop.append(panel);
    root.append(backdrop);
  }

  function render(nextSnapshot) {
    if (core.sameIdentity(snapshot, nextSnapshot)) return;
    snapshot = nextSnapshot;
    errorText = "";
    drafts = snapshot.request.action_requests.map((action, index) => {
      const allowed = core.reviewConfigFor(snapshot.request, index)?.allowed_decisions ?? [];
      const type = allowed.includes("approve")
        ? "approve"
        : allowed.find((item) => ["edit", "reject"].includes(item));
      return { type, args: structuredClone(action.args), reason: "" };
    });
    if (drafts.some((draft) => !draft.type)) {
      snapshot = null;
      return removeUi();
    }
    paint();
  }

  async function submit() {
    if (submitting || !snapshot) return;
    submitting = true;
    errorText = "";
    paint();
    try {
      const decisions = snapshot.request.action_requests.map((action, index) => {
        const draft = drafts[index];
        const allowed = core.reviewConfigFor(snapshot.request, index)?.allowed_decisions ?? [];
        if (!allowed.includes(draft.type)) throw new TypeError("该操作不允许当前决定");
        return core.buildDecision(action, draft.type, draft.args, draft.reason);
      });
      const context = core.parseStudioLocation(location.href);
      if (!context) throw new TypeError("当前页面不是受支持的 Studio thread");
      const response = await send({
        type: "studio_hitl.resume",
        threadId: context.threadId,
        expectedIdentity: snapshot.identity,
        decisions,
      });
      if (!response?.ok) throw new Error(response?.error?.message ?? "审批提交失败");
      activeRunId = response.run.runId;
      snapshot = null;
      showStatus("审批已提交，Agent 正在继续执行…");
    } catch (error) {
      errorText = error instanceof Error ? error.message : "审批提交失败";
      paint();
    } finally {
      submitting = false;
    }
  }

  async function poll() {
    try {
      const context = core.parseStudioLocation(location.href);
      if (!context) {
        snapshot = null;
        activeRunId = null;
        removeUi();
        return;
      }
      if (activeRunId) {
        const response = await send({ type: "studio_hitl.run", threadId: context.threadId, runId: activeRunId });
        if (!response?.ok) throw new Error(response?.error?.message ?? "无法读取 run 状态");
        if (["success", "error", "timeout", "interrupted"].includes(response.run.status)) {
          location.reload();
          return;
        }
        showStatus("审批已提交，Agent 正在继续执行…");
        return;
      }
      const response = await send({ type: "studio_hitl.get", threadId: context.threadId });
      if (!response?.ok) throw new Error(response?.error?.message ?? "无法读取 HITL 状态");
      if (response.snapshot) render(response.snapshot);
      else {
        snapshot = null;
        removeUi();
      }
    } catch (error) {
      if (snapshot) {
        errorText = error instanceof Error ? error.message : "HITL 状态读取失败";
        paint();
      }
    } finally {
      setTimeout(poll, document.hidden ? 5000 : 1000);
    }
  }

  poll();
})();
```

- [ ] **Step 5: 运行 Node 检查确认 GREEN**

Run: `node --test showcases/studio_hitl_extension/self_check.mjs`

Expected: 9 tests PASS，manifest 权限精确且 content script 不含危险 DOM API。

- [ ] **Step 6: 提交扩展 UI**

```bash
git add showcases/studio_hitl_extension/manifest.json showcases/studio_hitl_extension/content.js showcases/studio_hitl_extension/self_check.mjs
git commit -m "feat: render Studio HITL approval forms"
```

---

### Task 4: 增加确定性离线 HITL showcase

**Files:**
- Create: `showcases/studio_hitl_extension/graph.py`
- Create: `tests/tdd/studio-hitl-extension/test_graph.py`
- Modify: `langgraph.showcase.json`
- Modify: `tests/tdd/studio-evolution-showcase/test_graph.py`

**Interfaces:**
- Consumes: LangGraph `interrupt()` 与 Agent Server 注入的 checkpoint。
- Produces: `studio-hitl-showcase`，input 为 `{"scenario":"single"}` 或 `{"scenario":"multi"}`，resume 后 state 的 `result` 原样保存 HITL response。

- [ ] **Step 1: 写 showcase Graph 失败测试**

创建 `tests/tdd/studio-hitl-extension/test_graph.py`：

```python
from importlib import import_module

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


def _graph():
    module = import_module("showcases.studio_hitl_extension.graph")
    return module.build_graph(checkpointer=InMemorySaver())


def test_single_showcase_interrupts_with_nested_typed_args() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "single-hitl-showcase"}}
    result = graph.invoke({"scenario": "single"}, config=config)

    request = result["__interrupt__"][0].value
    assert [item["name"] for item in request["action_requests"]] == ["execute"]
    assert request["action_requests"][0]["args"] == {
        "command": "python -m pytest -q",
        "timeout_seconds": 30,
        "enabled": True,
        "env": {"MODE": "mock"},
        "paths": ["tests/core"],
    }

    decision = {
        "decisions": [
            {
                "type": "edit",
                "edited_action": {
                    "name": "execute",
                    "args": {
                        "command": "python -m pytest -q",
                        "timeout_seconds": 45,
                        "enabled": False,
                        "env": {"MODE": "mock"},
                        "paths": ["tests/core"],
                    },
                },
            }
        ]
    }
    resumed = graph.invoke(Command(resume=decision), config=config)
    assert resumed["result"] == decision


def test_multi_showcase_preserves_action_order() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "multi-hitl-showcase"}}
    result = graph.invoke({"scenario": "multi"}, config=config)
    request = result["__interrupt__"][0].value
    assert [item["name"] for item in request["action_requests"]] == ["execute", "write_file"]
    response = {
        "decisions": [
            {"type": "approve"},
            {"type": "reject", "message": "不写文件"},
        ]
    }
    assert graph.invoke(Command(resume=response), config=config)["result"] == response
```

同时先修改 `tests/tdd/studio-evolution-showcase/test_graph.py` 的 config 期望值：

```python
assert captured["config"]["graphs"] == {
    "studio-evolution-showcase": "./showcases/studio_evolution/graph.py:graph",
    "studio-hitl-showcase": "./showcases/studio_hitl_extension/graph.py:graph",
}
```

- [ ] **Step 2: 运行定向 pytest 确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/studio-hitl-extension \
  tests/tdd/studio-evolution-showcase
```

Expected: FAIL，`showcases.studio_hitl_extension.graph` 不存在且 config 缺少新 graph。

- [ ] **Step 3: 实现无 Provider showcase Graph**

创建 `graph.py`：

```python
from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class ShowcaseState(TypedDict, total=False):
    scenario: Literal["single", "multi"]
    result: dict[str, Any]


_ACTIONS = [
    {
        "name": "execute",
        "description": "执行离线测试命令",
        "args": {
            "command": "python -m pytest -q",
            "timeout_seconds": 30,
            "enabled": True,
            "env": {"MODE": "mock"},
            "paths": ["tests/core"],
        },
    },
    {
        "name": "write_file",
        "description": "写入本地演示文件",
        "args": {"path": "notes.txt", "content": "hello"},
    },
]
_REVIEWS = [
    {"action_name": item["name"], "allowed_decisions": ["approve", "edit", "reject"]}
    for item in _ACTIONS
]


def _review(state: ShowcaseState) -> dict[str, Any]:
    count = 2 if state.get("scenario") == "multi" else 1
    result = interrupt(
        {
            "action_requests": _ACTIONS[:count],
            "review_configs": _REVIEWS[:count],
        }
    )
    return {"result": result}


def build_graph(*, checkpointer=None):
    builder = StateGraph(ShowcaseState)
    builder.add_node("review", _review)
    builder.add_edge(START, "review")
    builder.add_edge("review", END)
    return builder.compile(checkpointer=checkpointer, name="studio-hitl-showcase")


graph = build_graph()
```

- [ ] **Step 4: 注册 showcase Graph**

把 `langgraph.showcase.json` 的 `graphs` 改为：

```json
{
  "studio-evolution-showcase": "./showcases/studio_evolution/graph.py:graph",
  "studio-hitl-showcase": "./showcases/studio_hitl_extension/graph.py:graph"
}
```

- [ ] **Step 5: 运行定向 pytest 确认 GREEN**

Run Task 4 Step 2 的同一命令。

Expected: 全部 PASS；测试不读取 `.env`、不访问网络或 Provider。

- [ ] **Step 6: 提交离线 showcase**

```bash
git add \
  showcases/studio_hitl_extension/graph.py \
  langgraph.showcase.json \
  tests/tdd/studio-hitl-extension/test_graph.py \
  tests/tdd/studio-evolution-showcase/test_graph.py
git commit -m "test: add offline Studio HITL showcase"
```

---

### Task 5: 文档、总验证与 Chrome 验收

**Files:**
- Create: `showcases/studio_hitl_extension/README.md`

**Interfaces:**
- Consumes: Tasks 1–4 的扩展目录、showcase config 和单实例 server wrapper。
- Produces: 用户可执行的安装、验证和卸载步骤；不改变生产 authority。

- [ ] **Step 1: 写 README**

README 必须包含以下可直接执行的内容：

```markdown
# Studio HITL Chrome 扩展

仅用于本机 `http://127.0.0.1:8089` 的 LangSmith Studio 开发调试。扩展把标准
`action_requests` / `review_configs` interrupt 渲染成表单，不修改生产 Agent 协议。

## 安装

1. 打开 `chrome://extensions`。
2. 启用“开发者模式”。
3. 选择“加载已解压的扩展程序”。
4. 选择仓库中的 `showcases/studio_hitl_extension/`。
5. 刷新已打开的 Studio 页面。

## 离线演示

先停止 PyCharm 管理的当前 `8089` 服务；不要并行启动第二套服务。然后从仓库根目录运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend dev \
  --config langgraph.showcase.json \
  --host 127.0.0.1 \
  --port 8089 \
  --no-env-file
```

Studio 选择 `studio-hitl-showcase`。输入 `{"scenario":"single"}` 验证嵌套字段编辑；输入
`{"scenario":"multi"}` 验证多个 action 一次提交。

## 卸载

在 `chrome://extensions` 删除扩展并刷新 Studio。扩展未运行时，Studio 原始 JSON resume 仍可使用。
```

- [ ] **Step 2: 运行全部自动验证**

```bash
node --test showcases/studio_hitl_extension/self_check.mjs

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/studio-hitl-extension \
  tests/tdd/studio-evolution-showcase

git diff --check -- \
  showcases/studio_hitl_extension \
  langgraph.showcase.json \
  tests/tdd/studio-hitl-extension \
  tests/tdd/studio-evolution-showcase/test_graph.py
```

Expected: Node 9 tests PASS；定向 pytest 全部 PASS；`git diff --check` 无输出。若实际 Node test 数量因合并相关断言略少或略多，以所有已列结构化场景 PASS 为准，不以固定计数伪造成功。

- [ ] **Step 3: 检查唯一 `8089`，不并行启动服务**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend dev \
  --config langgraph.showcase.json \
  --host 127.0.0.1 \
  --port 8089 \
  --no-env-file
```

Expected: 当前 PyCharm 服务仍运行时，单实例锁或端口检查明确拒绝启动。请用户停止 PyCharm 管理的服务后重试同一命令；不得换端口启动并行 Server。

- [ ] **Step 4: 完成 Chrome 手工验收**

在用户加载解压扩展并启动 showcase 后逐项验证：

1. Studio 普通页面无浮层。
2. `single` 触发一个卡片，字符串、数字、布尔、数组、对象均有字段控件。
3. 把 `timeout_seconds` 改为 `45`、`enabled` 改为 false；提交后 state `result` 保持 number/bool。
4. 新 thread 分别验证 approve 与带原因 reject。
5. `multi` 显示两个 action，未全部决定时不能提交；提交后 decision 顺序不变。
6. 在另一个 Studio 标签先恢复同一 interrupt；旧标签提交时显示 stale error 且无第二次 run。
7. 临时停止 `8089`；浮层显示网络错误且不自动重试。
8. 恢复或下一次中断后 Studio 页面只 reload 一次，Trace 显示最新 checkpoint。

若本轮无法获得用户的 Chrome 加载操作或无法切换唯一 `8089`，记录“Chrome 手工验收待用户完成”，不得声称端到端通过；自动验证仍需全部完成。

- [ ] **Step 5: 复核 production reload 与 authority**

本任务没有修改 `src/assistant_agent/**`、当前 `docs/*.md` authority、`docs/authority.toml` 或 core invariant，因此不机械运行全量 core pytest 和文档 authority validator。恢复 PyCharm 管理的生产 `8089` 后，确认：

```bash
curl -fsS http://127.0.0.1:8089/ok
```

Expected: `{"ok":true}`。真实 Provider 调用次数必须为 0。

- [ ] **Step 6: 只提交 README**

```bash
git add showcases/studio_hitl_extension/README.md
git commit -m "docs: document Studio HITL extension"
```

- [ ] **Step 7: 最终范围检查**

```bash
git status --short
git log -5 --oneline
```

Expected: 本任务提交只包含 File Map 中的实现、showcase、临时 TDD 和 README；既有用户改动保持原状。设计与计划文档按仓库规则保持未提交，除非用户另行要求。

最终汇报固定包含：

```text
Core invariant: unchanged.
Tests: added tests/tdd/studio-hitl-extension for temporary RED/GREEN; user may delete the directory manually.
Provider: real Provider calls = 0.
Chrome: <逐项写明已通过，或明确写待用户手工验收>.
```

(function () {
  "use strict";

  const core = globalThis.StudioHitlCore;
  let snapshot = null;
  let drafts = [];
  let activeRunId = null;
  let pageThreadId = null;
  let dismissedIdentity = null;
  let host = null;
  let shadow = null;
  let submitting = false;
  let reloading = false;
  let errorText = "";

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const send = (message) =>
    new Promise((resolve, reject) => {
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
      :host{all:initial}.backdrop{position:fixed;inset:0;z-index:2147483647;background:#0f172a99;display:grid;place-items:center;padding:24px;font:14px system-ui,sans-serif}
      .panel{box-sizing:border-box;width:min(760px,100%);max-height:calc(100vh - 48px);overflow:auto;background:#fff;color:#111827;border-radius:16px;box-shadow:0 24px 80px #0006;padding:24px}
      h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:0 0 8px}.action{border:1px solid #d1d5db;border-radius:12px;padding:16px;margin:12px 0}.field{display:grid;gap:6px;margin:10px 0}.nested{border-left:3px solid #e5e7eb;padding-left:12px}
      label{font-weight:600}input,textarea,select{box-sizing:border-box;width:100%;border:1px solid #9ca3af;border-radius:8px;padding:8px;font:inherit}input[type=checkbox]{width:auto}.choices,.footer{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
      button{border:1px solid #9ca3af;border-radius:8px;background:#fff;padding:8px 12px;cursor:pointer}button.primary{background:#2563eb;color:#fff;border-color:#2563eb}button:disabled,input:disabled,textarea:disabled,select:disabled{opacity:.5;cursor:not-allowed}.error{color:#b91c1c}.changed{outline:2px solid #f59e0b}.status{position:fixed;right:24px;bottom:24px;z-index:2147483647;background:#111827;color:#fff;border-radius:10px;padding:12px 16px;font:14px system-ui,sans-serif}
    `;
    shadow.append(style);
    return shadow;
  }

  function removeUi() {
    if (host) host.remove();
    host = null;
    shadow = null;
  }

  function clearViews() {
    if (!shadow) return;
    for (const node of shadow.querySelectorAll(".backdrop,.status")) node.remove();
  }

  function showStatus(text) {
    const root = ensureShadow();
    clearViews();
    const status = element("div", "status", text);
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    root.append(status);
  }

  function setPath(root, path, value) {
    let cursor = root;
    for (const part of path.slice(0, -1)) cursor = cursor[part];
    cursor[path[path.length - 1]] = value;
  }

  function renderField(
    container,
    actionIndex,
    label,
    value,
    path,
    rootSchema,
    editable,
  ) {
    const schema = core.schemaAtPath(rootSchema, path);
    const kind = core.fieldKind(value, schema);
    const nested = kind === "array" || kind === "object";
    const row = element("div", nested ? "field nested" : "field");
    const labelNode = element("label", null, String(label));
    row.append(labelNode);

    if (nested) {
      for (const [key, child] of Object.entries(value)) {
        renderField(
          row,
          actionIndex,
          key,
          child,
          [...path, Array.isArray(value) ? Number(key) : key],
          rootSchema,
          editable,
        );
      }
    } else {
      const hasEnum = Array.isArray(schema && schema.enum);
      const input = hasEnum
        ? element("select")
        : kind === "string" && (value.includes("\n") || value.length > 120)
          ? element("textarea")
          : element("input");
      const inputId = `assistant-agent-hitl-${actionIndex}-${path.join("-")}`;
      input.id = inputId;
      input.disabled = submitting || !editable;
      labelNode.htmlFor = inputId;

      if (hasEnum) {
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
        input.step = schema && schema.type === "integer" ? "1" : "any";
        input.value = String(value);
      } else {
        input.value = kind === "null" ? "null" : value;
      }

      if (editable) {
        input.addEventListener("change", () => {
          const fieldKey = JSON.stringify(path);
          const invalid = (message) => {
            drafts[actionIndex].invalidFields.add(fieldKey);
            input.setCustomValidity(message);
          };
          let next;
          if (hasEnum) next = JSON.parse(input.value);
          else if (kind === "boolean") next = input.checked;
          else if (kind === "number") {
            if (!input.value.trim()) {
              invalid("请输入有效数字");
              return;
            }
            next = Number(input.value);
            if (!Number.isFinite(next)) {
              invalid("请输入有效数字");
              return;
            }
            if (schema && schema.type === "integer" && !Number.isInteger(next)) {
              invalid("请输入整数");
              return;
            }
          } else if (kind === "null") {
            try {
              next = JSON.parse(input.value);
            } catch {
              invalid("请输入有效 JSON 值");
              return;
            }
          } else next = input.value;
          input.setCustomValidity("");
          drafts[actionIndex].invalidFields.delete(fieldKey);
          setPath(drafts[actionIndex].args, path, next);
          input.classList.add("changed");
        });
      }
      row.append(input);
    }
    container.append(row);
  }

  function choose(actionIndex, type) {
    drafts[actionIndex].type = type;
    drafts[actionIndex].invalidFields.clear();
    errorText = "";
    paint();
  }

  function paintAction(panel, action, actionIndex) {
    const config = core.reviewConfigFor(snapshot.request, actionIndex);
    const allowed = (config && Array.isArray(config.allowed_decisions)
      ? config.allowed_decisions
      : []
    ).filter((item) => ["approve", "edit", "reject"].includes(item));
    const card = element("section", "action");
    card.append(element("h2", null, action.name));
    if (action.description) card.append(element("p", null, action.description));

    const choices = element("div", "choices");
    for (const type of allowed) {
      const labels = { approve: "批准", edit: "编辑参数", reject: "拒绝" };
      const selected = drafts[actionIndex].type === type;
      const button = element("button", selected ? "primary" : "", labels[type]);
      button.type = "button";
      button.disabled = submitting;
      button.setAttribute("aria-pressed", String(selected));
      button.addEventListener("click", () => choose(actionIndex, type));
      choices.append(button);
    }
    card.append(choices);

    const editing = drafts[actionIndex].type === "edit";
    const visibleArgs = editing ? drafts[actionIndex].args : action.args;
    for (const [key, value] of Object.entries(visibleArgs)) {
      renderField(
        card,
        actionIndex,
        key,
        value,
        [key],
        config && config.args_schema ? config.args_schema : null,
        editing,
      );
    }
    if (drafts[actionIndex].type === "reject") {
      const row = element("div", "field");
      const labelNode = element("label", null, "拒绝原因");
      const textarea = element("textarea");
      textarea.id = `assistant-agent-hitl-reason-${actionIndex}`;
      labelNode.htmlFor = textarea.id;
      textarea.required = true;
      textarea.value = drafts[actionIndex].reason;
      textarea.disabled = submitting;
      textarea.addEventListener("input", () => {
        drafts[actionIndex].reason = textarea.value;
      });
      row.append(labelNode, textarea);
      card.append(row);
    }
    panel.append(card);
  }

  function paint() {
    if (!snapshot || snapshot.identity === dismissedIdentity) {
      removeUi();
      return;
    }
    const root = ensureShadow();
    clearViews();
    const backdrop = element("div", "backdrop");
    const panel = element("main", "panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "assistant-agent-hitl-title");
    const title = element("h1", null, "需要你的批准");
    title.id = "assistant-agent-hitl-title";
    panel.append(title);
    for (const [index, action] of snapshot.request.action_requests.entries()) {
      paintAction(panel, action, index);
    }
    if (errorText) {
      const error = element("p", "error", errorText);
      error.setAttribute("role", "alert");
      panel.append(error);
    }
    const footer = element("div", "footer");
    const button = element(
      "button",
      "primary",
      submitting ? "正在提交…" : "提交全部决定",
    );
    button.type = "button";
    button.disabled = submitting || drafts.some((draft) => !draft.type);
    button.addEventListener("click", submit);
    footer.append(button);
    const fallback = element("button", null, "使用 Studio 原界面");
    fallback.type = "button";
    fallback.disabled = submitting;
    fallback.addEventListener("click", () => {
      dismissedIdentity = snapshot.identity;
      removeUi();
    });
    footer.append(fallback);
    panel.append(footer);
    backdrop.append(panel);
    root.append(backdrop);
  }

  function render(nextSnapshot) {
    if (nextSnapshot.identity === dismissedIdentity) {
      snapshot = nextSnapshot;
      removeUi();
      return;
    }
    if (core.sameIdentity(snapshot, nextSnapshot)) return;
    snapshot = nextSnapshot;
    dismissedIdentity = null;
    errorText = "";
    drafts = snapshot.request.action_requests.map((action, index) => {
      const config = core.reviewConfigFor(snapshot.request, index);
      const allowed = config && Array.isArray(config.allowed_decisions)
        ? config.allowed_decisions
        : [];
      return {
        type: null,
        args: structuredClone(action.args),
        reason: "",
        invalidFields: new Set(),
        supported: allowed.some((item) =>
          ["approve", "edit", "reject"].includes(item),
        ),
      };
    });
    if (drafts.some((draft) => !draft.supported)) {
      snapshot = null;
      removeUi();
      return;
    }
    paint();
  }

  function responseError(response, fallback) {
    if (response && response.code === "stale_interrupt") return "审批状态已变化，请刷新后重试";
    return response && response.code ? `${fallback}（${response.code}）` : fallback;
  }

  async function submit() {
    if (submitting || !snapshot) return;
    errorText = "";
    try {
      const decisions = snapshot.request.action_requests.map((action, index) => {
        const draft = drafts[index];
        const config = core.reviewConfigFor(snapshot.request, index);
        const allowed = config && Array.isArray(config.allowed_decisions)
          ? config.allowed_decisions
          : [];
        if (!allowed.includes(draft.type)) throw new TypeError("该操作不允许当前决定");
        if (draft.type === "edit" && draft.invalidFields.size) {
          throw new TypeError("参数输入无效，已恢复上次有效值");
        }
        return core.buildDecision(action, draft.type, draft.args, draft.reason);
      });
      const context = core.parseStudioLocation(location.href);
      if (!context) throw new TypeError("当前页面不是受支持的 Studio thread");
      submitting = true;
      paint();
      const response = await send({
        type: "studio_hitl.resume",
        threadId: context.threadId,
        expectedIdentity: snapshot.identity,
        decisions,
      });
      if (response && response.code === "stale_interrupt") {
        snapshot = null;
        drafts = [];
        showStatus("审批状态已变化，正在重新读取…");
        return;
      }
      if (!response || !response.ok) {
        throw new Error(responseError(response, "审批提交失败"));
      }
      activeRunId = response.runId;
      snapshot = null;
      showStatus("审批已提交，Agent 正在继续执行…");
    } catch (error) {
      errorText = error instanceof Error ? error.message : "审批提交失败";
      for (const draft of drafts) draft.invalidFields.clear();
    } finally {
      submitting = false;
      if (snapshot) paint();
    }
  }

  function resetPage(threadId) {
    pageThreadId = threadId;
    snapshot = null;
    drafts = [];
    activeRunId = null;
    dismissedIdentity = null;
    errorText = "";
    removeUi();
  }

  async function poll() {
    let delay = 1000;
    try {
      const context = core.parseStudioLocation(location.href);
      if (!context) {
        if (pageThreadId !== null) resetPage(null);
        return;
      }
      delay = document.hidden ? 5000 : 1000;
      if (pageThreadId !== context.threadId) resetPage(context.threadId);

      if (activeRunId) {
        const response = await send({
          type: "studio_hitl.run",
          threadId: context.threadId,
          runId: activeRunId,
        });
        if (!response || !response.ok) {
          throw new Error(responseError(response, "无法读取 run 状态"));
        }
        if (["success", "error", "timeout", "interrupted"].includes(response.status)) {
          reloading = true;
          location.reload();
          return;
        }
        showStatus("审批已提交，Agent 正在继续执行…");
        return;
      }

      const response = await send({
        type: "studio_hitl.get",
        threadId: context.threadId,
      });
      if (!response || !response.ok) {
        throw new Error(responseError(response, "无法读取 HITL 状态"));
      }
      if (response.snapshot) render(response.snapshot);
      else {
        snapshot = null;
        removeUi();
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "HITL 状态读取失败";
      if (snapshot) {
        errorText = message;
        paint();
      } else if (activeRunId) showStatus(message);
    } finally {
      if (!reloading) setTimeout(poll, delay);
    }
  }

  poll();
})();

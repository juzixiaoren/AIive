import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";
import ts from "typescript";

const dom = new JSDOM("", { url: "https://aiive.example" });
Object.assign(globalThis, {
  window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
// jsdom has the dialog element but omits its top-layer methods.
dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };

const require = createRequire(import.meta.url);
function moduleUrl(path) {
  const source = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText.replace(/from\s+["']([^"']+)["']/g, (_, specifier) => {
    const url = specifier === "../api/client"
      ? moduleUrl("../src/api/client.ts")
      : pathToFileURL(require.resolve(specifier)).href;
    return `from ${JSON.stringify(url)}`;
  });
  return `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
}

const { act, createElement } = await import("react");
const { createRoot } = await import("react-dom/client");
const { default: ContextInspector } = await import(moduleUrl("../src/pages/ContextInspector.tsx"));

const preview = (label) => `${label}\n第二行\n\n第四行`;
const item = (label, itemId, truncated = true) => ({
  item_id: itemId, kind: "tool_result", source: "tools", trust_level: "trusted", token_estimate: 50,
  content_preview: truncated ? preview(label) : label,
  preview_length: truncated ? 100 : label.length, preview_truncated: truncated, has_detail: true,
});
const snapshot = (id) => ({
  snapshot_id: id, stable_prefix_hash: "hash", token_total: 100, created_at: "2026-11-20T04:30:00Z",
  injected_memory_ids: [], context_items: [item(id, "result:0"), item("第二项", "result:1"), item("短内容", "short", false)],
});
const detail = (snapshotId, itemId, content) => ({
  snapshot_id: snapshotId, item_id: itemId, full_content: content, content_length: Array.from(content).length,
});

function setup(t) {
  const requests = [];
  t.mock.method(globalThis, "fetch", (path, options) => new Promise((resolve, reject) => {
    requests.push({ url: new URL(path, window.location), signal: options.signal, resolve, reject });
  }));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  t.after(async () => {
    await act(() => root.unmount());
    container.remove();
  });
  const render = (traceId) => act(() => root.render(createElement(ContextInspector, { traceId })));
  const click = (element) => act(() => element.click());
  const buttons = (label, parent = container) =>
    [...parent.querySelectorAll("button")].filter((button) => button.textContent.trim() === label);
  const dialog = () => container.querySelector("dialog[open]");
  const reply = (index, data) => act(() => requests[index].resolve(Response.json(data)));
  const pick = (value) => act(() => {
    const select = container.querySelector("select");
    select.value = value;
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
  return { requests, container, render, click, buttons, dialog, reply, pick };
}

test("initial summaries are lazy, the dialog copies the whole response, and reopening uses cache", async (t) => {
  const ui = setup(t);
  const copied = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true, value: { writeText: async (value) => copied.push(value) },
  });
  await ui.render("trace");
  await ui.reply(0, { trace_id: "trace", snapshots: [snapshot("snapshot-a")] });
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.container.querySelector("li pre").textContent, preview("snapshot-a"));
  assert.equal(ui.buttons("查看更多").length, 2);

  await ui.click(ui.buttons("查看更多")[0]);
  assert.equal(ui.requests[1].url.pathname, "/api/context-runs/trace/items/result%3A0");
  assert.equal(ui.requests[1].url.searchParams.get("snapshot_id"), "snapshot-a");
  assert.match(ui.dialog().textContent, /正在加载完整内容/);
  assert.equal(ui.dialog().querySelector("pre"), null);
  await ui.click(ui.buttons("关闭", ui.dialog())[0]);
  await ui.click(ui.buttons("查看更多")[0]);
  assert.equal(ui.requests.length, 2);

  const body = "真正的正文😀\n" + "字".repeat(4000) + "\n最后一行";
  await ui.reply(1, detail("snapshot-a", "result:0", body));
  assert.equal(ui.dialog().querySelector("pre").textContent, body);
  assert.match(ui.dialog().textContent, new RegExp(`字符数${Array.from(body).length}`));
  await ui.click(ui.buttons("复制完整内容", ui.dialog())[0]);
  assert.deepEqual(copied, [body]);
  await ui.click(ui.buttons("关闭", ui.dialog())[0]);
  await ui.click(ui.buttons("查看更多")[0]);
  assert.equal(ui.dialog().querySelector("pre").textContent, body);
  assert.equal(ui.requests.length, 2);
});

test("failure is shown inside the dialog, retry makes a new request, and mismatched details are rejected", async (t) => {
  const ui = setup(t);
  await ui.render("trace");
  await ui.reply(0, { trace_id: "trace", snapshots: [snapshot("snapshot-a")] });
  await ui.click(ui.buttons("查看更多")[0]);
  await act(() => ui.requests[1].reject(new Error("offline")));
  assert.match(ui.dialog().querySelector('[role="alert"]').textContent, /内容加载失败.*offline/);
  assert.equal(ui.dialog().querySelector("pre"), null);

  await ui.click(ui.buttons("重试", ui.dialog())[0]);
  assert.equal(ui.requests[2].url.href, ui.requests[1].url.href);
  assert.match(ui.dialog().textContent, /正在加载完整内容/);
  await ui.reply(2, detail("wrong-snapshot", "result:0", "wrong body"));
  assert.match(ui.dialog().querySelector('[role="alert"]').textContent, /其他上下文项/);
  assert.equal(ui.dialog().querySelector("pre"), null);
  await ui.click(ui.buttons("重试", ui.dialog())[0]);
  await ui.reply(3, detail("snapshot-a", "result:0", "retried body"));
  assert.equal(ui.dialog().querySelector("pre").textContent, "retried body");
});

test("parallel items, snapshot changes and trace changes isolate late responses", async (t) => {
  const ui = setup(t);
  await ui.render("trace");
  await ui.reply(0, { trace_id: "trace", snapshots: [snapshot("snapshot-a"), snapshot("snapshot-b")] });

  await ui.click(ui.buttons("查看更多")[0]);
  await ui.click(ui.buttons("关闭", ui.dialog())[0]);
  await ui.click(ui.buttons("查看更多")[1]);
  await ui.reply(1, detail("snapshot-a", "result:0", "first body"));
  assert.match(ui.dialog().textContent, /正在加载完整内容/);

  await ui.pick("snapshot-b");
  assert.equal(ui.requests[2].signal.aborted, true);
  assert.equal(ui.dialog(), null);
  await ui.click(ui.buttons("查看更多")[1]);
  await ui.pick("snapshot-a");
  assert.equal(ui.requests[3].signal.aborted, true);
  await ui.click(ui.buttons("查看更多")[1]);
  await ui.reply(2, detail("snapshot-a", "result:1", "late result"));
  await ui.reply(3, detail("snapshot-b", "result:1", "other snapshot"));
  assert.match(ui.dialog().textContent, /正在加载完整内容/);

  await ui.click(ui.buttons("关闭", ui.dialog())[0]);
  await ui.click(ui.buttons("查看更多")[0]);
  assert.equal(ui.dialog().querySelector("pre").textContent, "first body");
  await ui.render("other-trace");
  assert.equal(ui.requests[4].signal.aborted, true);
  await ui.render("final-trace");
  assert.equal(ui.requests[5].signal.aborted, true);
  await ui.reply(5, { trace_id: "other-trace", snapshots: [snapshot("late-trace")] });
  await ui.reply(4, detail("snapshot-a", "result:1", "old trace body"));
  await ui.reply(6, { trace_id: "final-trace", snapshots: [snapshot("snapshot-a")] });
  assert.equal(ui.dialog(), null);
  assert.doesNotMatch(ui.container.textContent, /old trace body|late-trace|first body/);
  await ui.click(ui.buttons("查看更多")[0]);
  assert.match(ui.dialog().textContent, /正在加载完整内容/);
  assert.equal(ui.requests[7].url.pathname, "/api/context-runs/final-trace/items/result%3A0");
});

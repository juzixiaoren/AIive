import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { act, createElement } from "react";
import { createServer } from "vite";

async function setupChat(t) {
  const dom = new JSDOM("", { url: "https://aiive.example/" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: callback => setTimeout(callback, 0),
  });
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
  dom.window.HTMLElement.prototype.scrollTo = () => {};
  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); }
    close() {}
  };

  const vite = await createServer({
    configFile: false, optimizeDeps: { noDiscovery: true },
    esbuild: { jsx: "automatic" }, resolve: { dedupe: ["react", "react-dom"] },
    server: { middlewareMode: true, watch: null, hmr: false },
  });
  t.after(() => vite.close());
  const { createRoot } = await import("react-dom/client");
  const root = createRoot(document.body.appendChild(document.createElement("main")));
  t.after(async () => { await act(async () => root.unmount()); dom.window.close(); });
  const { default: ChatPage } = await vite.ssrLoadModule("/src/pages/ChatPage.tsx");
  return { sockets, render: () => act(async () => root.render(createElement(ChatPage))) };
}

test("chat can retry a failed handshake and recovers automatically when back online", async (t) => {
  const { sockets, render } = await setupChat(t);
  let attempts = 0;
  t.mock.method(globalThis, "fetch", async input => {
    switch (new URL(input, window.location).pathname) {
      case "/api/conversation/current":
        return ++attempts === 1
          ? Response.json({ detail: "服务暂时不可用" }, { status: 503 })
          : Response.json({ thread_id: "thread" });
      case "/api/threads/thread/messages":
        return Response.json({ messages: [], next_cursor: null, has_more: false });
      case "/api/conversation/status":
        return attempts === 2
          ? Response.json({ detail: "暂时无法读取执行状态" }, { status: 503 })
          : Response.json({ thread_id: "thread", state: "idle" });
      default:
        assert.fail(input);
    }
  });
  const retryButton = () => [...document.querySelectorAll("button")]
    .find(button => button.textContent === "重新同步对话");
  const composer = () => document.querySelector("textarea");

  await render();
  assert.equal(document.querySelector('[role="alert"]').textContent, "服务暂时不可用");
  assert.equal(composer().disabled, true);
  await act(async () => retryButton().click());
  assert.equal(document.querySelector('[role="alert"]').textContent, "暂时无法读取执行状态");
  assert.equal(composer().disabled, true);
  assert.equal(sockets.length, 0);

  await act(async () => {
    window.dispatchEvent(new window.Event("online"));
    window.dispatchEvent(new window.Event("online"));
  });
  assert.equal(attempts, 3);
  assert.equal(composer().disabled, false);
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.equal(retryButton(), undefined);
  assert.deepEqual(sockets.map(socket => socket.url), ["wss://aiive.example/ws/thread"]);
});

test("socket recovery keeps the expanded range and rechecks forgotten messages", async (t) => {
  const { sockets, render } = await setupChat(t);
  let recovered = false;
  const queries = [];
  const page = (texts, cursor) => Response.json({
    messages: texts.map(content => ({ event_id: content, content, role: "user" })),
    next_cursor: cursor, has_more: cursor !== null, visibility_revision: recovered ? 2 : 1,
  });
  t.mock.method(globalThis, "fetch", async input => {
    const url = new URL(input, window.location);
    if (url.pathname === "/api/conversation/current") return Response.json({ thread_id: "thread" });
    if (url.pathname === "/api/conversation/status") return Response.json({ thread_id: "thread", state: "idle" });
    assert.equal(url.pathname, "/api/threads/thread/messages");
    const query = [url.searchParams.get("before_sequence"), url.searchParams.get("from_sequence")];
    queries.push(query);
    if (query[0]) {
      return recovered ? page(["old visible message"], null)
        : page(["old visible message", "forgotten message"], 25);
    }
    return recovered ? page(["new reply"], 50) : page(["recent message"], 50);
  });
  const loadOlder = () => [...document.querySelectorAll("button")]
    .find(button => button.textContent === "加载更早消息");
  await render();
  await act(async () => loadOlder().click());
  assert.match(document.body.textContent, /old visible message.*forgotten message.*recent message/s);

  recovered = true;
  await act(async () => sockets[0].onmessage({
    data: JSON.stringify({ type: "execution_status", data: { thread_id: "thread", state: "idle" } }),
  }));
  assert.deepEqual(queries, [[null, null], ["50", null], [null, "25"], ["50", "25"]]);
  assert.match(document.body.textContent, /old visible message.*new reply/s);
  assert.doesNotMatch(document.body.textContent, /forgotten message/);
  assert.ok(loadOlder(), "the cursor into unopened history is preserved");
});

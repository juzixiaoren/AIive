import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { act, createElement } from "react";
import { createServer } from "vite";

test("chat can retry a failed handshake and recovers automatically when back online", async (t) => {
  const dom = new JSDOM("", { url: "https://aiive.example/" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
  dom.window.HTMLElement.prototype.scrollTo = () => {};
  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { sockets.push(url); }
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
  const { default: ChatPage } = await vite.ssrLoadModule("/src/pages/ChatPage.tsx");
  const retryButton = () => [...document.querySelectorAll("button")]
    .find(button => button.textContent === "重新同步对话");
  const composer = () => document.querySelector("textarea");

  await act(async () => root.render(createElement(ChatPage)));
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
  assert.deepEqual(sockets, ["wss://aiive.example/ws/thread"]);
});

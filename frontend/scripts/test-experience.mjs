import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { act, createElement } from "react";
import { createServer } from "vite";

test("review an older episode, retry a failed page and retain feedback on return", async (t) => {
  const dom = new JSDOM("", { url: "https://aiive.example/evolution" });
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
  const { createRoot } = await import("react-dom/client");
  const vite = await createServer({
    configFile: false, optimizeDeps: { noDiscovery: true }, esbuild: { jsx: "automatic" },
    server: { middlewareMode: true, watch: null, hmr: false },
  });
  t.after(() => vite.close());
  const { default: Page } = await vite.ssrLoadModule("/src/pages/SelfEvolutionPage.tsx");
  const root = createRoot(document.body.appendChild(document.createElement("main")));
  t.after(async () => { await act(() => root.unmount()); dom.window.close(); });
  const episode = (id, title) => ({
    id, title, kind: "turn", family: "document", outcome: "unknown",
    completed_at: "2026-11-27T10:00:00Z", uses: [], metrics: {},
  });
  const recent = episode("new", "概括正文");
  const older = episode("old", "检查附录");
  let attempts = 0;
  t.mock.method(globalThis, "fetch", async (input, init) => {
    const url = new URL(input, window.location);
    if (url.pathname === "/api/experience") {
      return Response.json({ episodes: [recent], episodes_next_cursor: "a+b|old", candidates: [], insights: [] });
    }
    if (url.pathname === "/api/experience/episodes") {
      assert.equal(url.searchParams.get("cursor"), "a+b|old");
      if (++attempts === 1) throw new Error("连接中断");
      return Response.json({ items: [older], next_cursor: null });
    }
    if (url.pathname === "/api/experience/episodes/old/feedback") {
      assert.deepEqual(JSON.parse(init.body), { kind: "rework" });
      return Response.json({ ...older, feedback: "rework", outcome: "rework", result_preview: "这是附录摘要" });
    }
    assert.equal(url.pathname, "/api/experience/episodes/old");
    return Response.json({ ...older, result_preview: "这是附录摘要" });
  });
  const button = text => [...document.querySelectorAll("button")].find(b => b.textContent.includes(text));
  const click = text => act(() => button(text).click());

  await act(() => root.render(createElement(Page, { onOpenWork: () => {} })));
  assert.ok(button("概括正文"));
  await click("加载更早的经历");
  assert.match(document.querySelector('[role="alert"]').textContent, /连接中断/);
  await click("加载更早的经历");
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.equal(button("加载更早的经历"), undefined);
  await click("检查附录");
  assert.match(document.querySelector("article").textContent, /当时的答复.*这是附录摘要/s);
  await click("返工");
  assert.equal(button("返工").getAttribute("aria-pressed"), "true");
  await click("返回列表");
  assert.match(button("检查附录").textContent, /返工/);
  assert.ok(button("概括正文"));
});

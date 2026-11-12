import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { test } from "node:test";
import ts from "typescript";

const cache = new Map();
function moduleUrl(file) {
  if (cache.has(file.href)) return cache.get(file.href);
  let source = ts.transpileModule(readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  // Supply only the build-time backend address; the production chat transport stays intact.
  source = source.replace(
    "import.meta.env.VITE_AIIVE_API_BASE_URL",
    JSON.stringify("https://api.example"),
  );
  source = source.replace(/from\s+(['"])([^'"]+)\1/g, (_, quote, specifier) => {
    const target = specifier.startsWith(".")
      ? moduleUrl(new URL(`${specifier}.ts`, file))
      : pathToFileURL(createRequire(file).resolve(specifier)).href;
    return `from ${JSON.stringify(target)}`;
  });
  const url = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
  cache.set(file.href, url);
  return url;
}
const load = (path) => import(moduleUrl(new URL(path, import.meta.url)));
const { WorkClient } = await load("../../shared/work/client.ts");
const { WorkController, mergeTimeline } = await load(
  "../../shared/work/controller.ts",
);
const { workCardsFrom } = await load("../../shared/work/types.ts");
const { performAttention, validatePlan } = await load(
  "../../shared/work/operations.ts",
);
const { downloadVerified, safeFilename, canPreview, CHUNK_BYTES } = await load(
  "../../shared/work/files.ts",
);
const { prepareNativeFile } = await load(
  "../../apps/android/src/work/nativeFiles.ts",
);
const { subscribeThread } = await load("../../shared/work/connection.ts");
const androidChat = await load("../../apps/android/src/api/chat.ts");
const webChat = await load("../src/api/chat.ts");
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const metadata = (bytes) => ({
  id: "file",
  name: "../危険\\report.txt",
  mime_type: "text/plain",
  content_size: bytes.length,
  content_hash: sha(bytes),
});
const json = (data) =>
  new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" },
  });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const view = (id) => ({
  task: { task_id: id },
  timeline: [{ id: "1", sequence: 1, title: "old" }],
  last_sequence: 5,
  truncated: { timeline: true },
});

test("distinct delegation cards restore once and never adopt reminder task IDs", () => {
  const card = {
    card_type: "agent_task_created",
    title: "Research",
    resource_refs: { task_id: "work" },
    payload_preview: { status: "queued" },
  };
  assert.deepEqual(
    workCardsFrom([
      card,
      card,
      { ...card, card_type: "task_created" },
      { ...card, resource_refs: {} },
    ]).map((c) => c.taskId),
    ["work"],
  );
});

test("every public operation uses the existing API; question rejection is step-scoped", async () => {
  const requests = [];
  const api = new WorkClient(async (path, init) => {
    requests.push([path, init?.body ? JSON.parse(init.body) : null]);
    return json({});
  });
  for (const bucket of ["attention", "active", "waiting", "ended"])
    await api.inbox(bucket, "cursor");
  await api.view("a/b");
  await api.timeline("a/b", 42);
  await performAttention(
    api,
    "task",
    {
      kind: "action_approval",
      approval_id: "ap",
      allowed_actions: ["approve", "deny"],
    },
    "deny",
  );
  await performAttention(
    api,
    "task",
    {
      kind: "goal_step_approval",
      step_id: "step",
      allowed_actions: ["approve_step"],
    },
    "approve_step",
  );
  await performAttention(
    api,
    "task",
    {
      kind: "goal_step_start",
      step_id: "step",
      allowed_actions: ["start_step"],
    },
    "start_step",
  );
  for (const intent of ["answer", "modify_plan", "reject"])
    await performAttention(
      api,
      "task",
      {
        kind: "goal_question",
        step_id: "step",
        question_id: "q",
        allowed_actions: [intent],
      },
      intent,
      "answer",
      { summary: "new" },
    );
  await api.input("task", "extra");
  await api.cancel("task");
  for (const action of [
    "confirm",
    "modify",
    "reject",
    "pause",
    "resume",
    "end",
  ])
    await api.goal("task", action);
  assert.ok(requests.some(([p]) => p === "agent-tasks/a%2Fb/work-view"));
  assert.ok(requests.some(([p]) => p.endsWith("after_sequence=42&limit=100")));
  assert.deepEqual(requests.find(([p]) => p === "approval/respond")[1], {
    approval_id: "ap",
    action: "deny",
  });
  const questions = requests.filter(([p]) => p.endsWith("/questions/answer"));
  assert.deepEqual(
    questions.map(([, body]) => body.intent),
    ["answer", "modify_plan", "reject"],
  );
  assert.ok(
    questions.every(
      ([, body]) => body.question_id === "q" && body.step_id === "step",
    ),
  );
  assert.throws(() =>
    performAttention(
      api,
      "task",
      { kind: "goal_question", allowed_actions: [] },
      "reject",
    ),
  );
});

test("plan validation rejects cycles and missing dependency IDs", () => {
  const plan = {
    summary: "goal",
    timezone: "Asia/Shanghai",
    steps: [
      { id: "a", title: "a", depends_on: ["b"] },
      { id: "b", title: "b", depends_on: ["a"] },
    ],
  };
  assert.throws(() => validatePlan(plan), /循环/);
  assert.throws(
    () => validatePlan({ ...plan, steps: [plan.steps[0]] }),
    /不存在/,
  );
});

test("refresh serializes duplicate signals, follows raw high water, and removes forgotten cache", async () => {
  let calls = 0,
    active = 0,
    max = 0,
    forgotten = false;
  const after = [];
  const api = {
    inbox: async () => {
      calls++;
      active++;
      max = Math.max(active, max);
      await tick();
      active--;
      return { items: [], next_cursor: null, has_more: false };
    },
    view: async (id) => ({
      ...view(id),
      timeline: forgotten ? [] : view(id).timeline,
    }),
    timeline: async (_id, cursor) => {
      after.push(cursor);
      return { items: [], next_sequence: cursor + 1, has_more: cursor === 5 };
    },
  };
  const store = new WorkController(api);
  store.select("task");
  await Promise.all([store.refresh(), store.refresh(), store.refresh()]);
  assert.equal(max, 1);
  assert.ok(calls <= 2);
  assert.deepEqual(after.slice(0, 2), [5, 6]);
  assert.equal(store.state.timeline.length, 1);
  forgotten = true;
  await store.refresh();
  assert.deepEqual(store.state.timeline, []);
  assert.equal(
    mergeTimeline([{ sequence: 1 }], [{ sequence: 1 }, { sequence: 2 }]).length,
    2,
  );
});

test("selection races and paginated duplicate items cannot cross tasks or buckets", async () => {
  let resolve;
  const gate = new Promise((r) => {
    resolve = r;
  });
  let count = 0;
  const store = new WorkController({
    inbox: async (_bucket, cursor) => {
      if (++count === 1) await gate;
      return {
        items: [{ task_id: cursor ? "b" : "a" }, { task_id: "a" }],
        next_cursor: cursor ? null : "next",
        has_more: !cursor,
      };
    },
    view: async (id) => view(id),
    timeline: async () => ({ items: [], next_sequence: 5, has_more: false }),
  });
  store.select("old");
  store.select("new");
  resolve();
  await store.refresh();
  assert.equal(store.state.view.task.task_id, "new");
  await store.more();
  assert.deepEqual(
    store.state.items.map((i) => i.task_id),
    ["a", "b"],
  );
  store.dispose();
  await store.refresh();
  assert.equal(count, 3);
});

test("work_changed is invalidation only; duplicates and stale versions do not refresh again", async () => {
  let reads = 0;
  const store = new WorkController({
    inbox: async () => {
      reads++;
      return { items: [], next_cursor: null, has_more: false };
    },
  });
  store.invalidate({
    task_id: "task",
    task_version: 2,
    event_sequence: 5,
    title: "untrusted",
  });
  await store.refresh();
  const before = reads;
  store.invalidate({ task_id: "task", task_version: 2, event_sequence: 5 });
  store.invalidate({ task_id: "task", task_version: 1, event_sequence: 4 });
  await tick();
  assert.equal(reads, before);
  assert.equal(store.state.view, null);
  store.invalidate({ task_id: "task", task_version: 3, event_sequence: 5 });
  await tick();
  assert.equal(reads, before + 1);
});

function chunkFetch(bytes, step = 700003, override = () => ({})) {
  const offsets = [];
  return {
    offsets,
    fetch: async (path) => {
      const query = new URL(`https://local/${path}`).searchParams;
      assert.equal(Number(query.get("max_bytes")), CHUNK_BYTES);
      const offset = Number(query.get("offset"));
      offsets.push(offset);
      const part = bytes.subarray(
        offset,
        Math.min(offset + step, bytes.length),
      );
      return new Response(part, {
        headers: {
          "X-AIive-Content-Hash": sha(bytes),
          "X-AIive-Next-Offset": String(offset + part.length),
          "X-AIive-Truncated": String(offset + part.length < bytes.length),
          ...override(offset),
        },
      });
    },
  };
}
test("artifact and evidence downloads advance by actual bytes and verify complete SHA256", async () => {
  const bytes = Buffer.alloc(CHUNK_BYTES * 2 + 91, 135);
  for (const kind of ["artifacts", "evidence"]) {
    const { fetch, offsets } = chunkFetch(bytes);
    const output = [];
    await downloadVerified(
      fetch,
      "t",
      kind,
      metadata(bytes),
      createHash("sha256"),
      {
        write: async (chunk) => output.push(chunk),
        abort: async () => assert.fail("unexpected abort"),
      },
    );
    assert.deepEqual(offsets, [0, 700003, 1400006]);
    assert.deepEqual(Buffer.concat(output), bytes);
  }
});
test("zero-length file is verified and unsafe types cannot be previewed", async () => {
  const bytes = Buffer.alloc(0),
    { fetch } = chunkFetch(bytes);
  await downloadVerified(
    fetch,
    "t",
    "artifacts",
    metadata(bytes),
    createHash("sha256"),
    { write: async () => {}, abort: async () => assert.fail() },
  );
  for (const mime_type of [
    "text/html",
    "image/svg+xml",
    "application/javascript",
    "application/octet-stream",
  ])
    assert.equal(canPreview({ ...metadata(bytes), mime_type }), false);
  for (const name of [
    "../../secret",
    "..\\..\\secret",
    "/tmp/file",
    "CON.txt",
    "NUL",
    "\u202eabc",
    "...",
    "a\r\nb",
  ])
    assert.ok(
      !/[\\/\r\n\u202e]/.test(safeFilename(name)) &&
        !/^\.+$/.test(safeFilename(name)),
    );
});
test("corruption, missing headers, stalled offsets, oversized chunks and network failure abort the sink", async () => {
  const bytes = Buffer.from("content");
  const cases = [
    async () => new Response(bytes),
    chunkFetch(bytes, 3, () => ({ "X-AIive-Next-Offset": "0" })).fetch,
    chunkFetch(bytes, 3, () => ({ "X-AIive-Truncated": "false" })).fetch,
    async () => {
      throw new Error("offline");
    },
    chunkFetch(Buffer.alloc(CHUNK_BYTES + 1), CHUNK_BYTES + 1).fetch,
    async () =>
      new Response(Buffer.from("corrupt"), {
        headers: {
          "X-AIive-Content-Hash": sha(bytes),
          "X-AIive-Next-Offset": "7",
          "X-AIive-Truncated": "false",
        },
      }),
  ];
  for (const fetch of cases) {
    let aborted = false;
    await assert.rejects(
      downloadVerified(
        fetch,
        "t",
        "evidence",
        metadata(bytes),
        createHash("sha256"),
        {
          write: async () => {},
          abort: async () => {
            aborted = true;
          },
        },
      ),
    );
    assert.equal(aborted, true);
  }
});

function nativeStorage() {
  const files = new Map(),
    calls = [];
  const fs = {
    writeFile: async (o) => {
      files.set(`${o.directory}/${o.path}`, Buffer.from(o.data, "base64"));
      calls.push(["write", o]);
    },
    appendFile: async (o) => {
      const key = `${o.directory}/${o.path}`;
      files.set(
        key,
        Buffer.concat([files.get(key), Buffer.from(o.data, "base64")]),
      );
      calls.push(["append", o]);
    },
    deleteFile: async (o) => {
      files.delete(`${o.directory}/${o.path}`);
      calls.push(["delete", o]);
    },
    mkdir: async (o) => {
      calls.push(["mkdir", o]);
    },
    rmdir: async (o) => {
      calls.push(["rmdir", o]);
    },
    copy: async (o) => {
      files.set(
        `${o.toDirectory}/${o.to}`,
        files.get(`${o.directory}/${o.from}`),
      );
      calls.push(["copy", o]);
    },
    getUri: async (o) => ({ uri: `file://cache/${o.path}` }),
    readFile: async (o) => ({
      data: files.get(`${o.directory}/${o.path}`).toString("base64"),
    }),
    checkPermissions: async () => ({ publicStorage: "granted" }),
    requestPermissions: async () => ({ publicStorage: "granted" }),
  };
  return { fs, files, calls };
}
test("native saves verified bytes to Documents and shares a cache URI only after verification", async () => {
  const bytes = Buffer.from("你好，成果"),
    { fs, files, calls } = nativeStorage(),
    shares = [];
  const handle = await prepareNativeFile(
    chunkFetch(bytes, 4).fetch,
    "t",
    "artifacts",
    metadata(bytes),
    undefined,
    undefined,
    fs,
    { share: async (options) => shares.push(options) },
  );
  assert.equal(calls.filter(([type]) => type === "copy").length, 0);
  const path = await handle.save();
  assert.ok(path.startsWith("Documents/AIive/"));
  const copy = calls.find(([type]) => type === "copy")[1];
  assert.equal(copy.toDirectory, "DOCUMENTS");
  assert.ok(!copy.to.includes("../"));
  assert.deepEqual([...files.values()][0], bytes);
  assert.equal(await (await handle.preview()).text(), bytes.toString());
  await handle.share();
  assert.match(shares[0].files[0], /^file:\/\/cache\/aiive-work\//);
  await handle.dispose();
  assert.equal(calls.filter(([type]) => type === "delete").length, 0); // recipient may still read the URI
});
test("native failures clean partial files and never call save or Share", async () => {
  const bytes = Buffer.from("expected"),
    { fs, files, calls } = nativeStorage();
  let shares = 0;
  await assert.rejects(
    prepareNativeFile(
      async () => {
        throw new Error("offline");
      },
      "t",
      "artifacts",
      metadata(bytes),
      undefined,
      undefined,
      fs,
      {
        share: async () => {
          shares++;
        },
      },
    ),
  );
  assert.equal(files.size, 0);
  assert.equal(shares, 0);
  assert.ok(calls.some(([t]) => t === "delete"));
  assert.ok(!calls.some(([t]) => t === "copy"));
});

test("native checksum and mid-download failure remove bytes already written to cache", async () => {
  const bytes = Buffer.from("expected");
  for (const failure of ["network", "hash"]) {
    const { fs, files, calls } = nativeStorage();
    let parts = 0;
    const fetch = async () => {
      if (++parts === 2 && failure === "network") throw new Error("offline");
      const chunk =
        failure === "hash" ? Buffer.from("tampered") : bytes.subarray(0, 4);
      return new Response(chunk, {
        headers: {
          "X-AIive-Content-Hash": sha(bytes),
          "X-AIive-Next-Offset": String(chunk.length),
          "X-AIive-Truncated": String(failure === "network"),
        },
      });
    };
    await assert.rejects(
      prepareNativeFile(
        fetch,
        "t",
        "artifacts",
        metadata(bytes),
        undefined,
        undefined,
        fs,
        { share: async () => assert.fail("unverified sharing") },
      ),
    );
    assert.ok(calls.some(([t]) => t === "append"));
    assert.equal(files.size, 0);
    assert.ok(!calls.some(([t]) => t === "copy"));
  }
});
test("thread reconnect, foreground and online refresh snapshots and dispose releases all sockets", async () => {
  const original = {
    WebSocket: globalThis.WebSocket,
    window: globalThis.window,
    document: globalThis.document,
  };
  const sockets = [];
  class Socket {
    constructor(url) {
      this.url = url;
      sockets.push(this);
    }
    send(message) {
      this.auth = JSON.parse(message);
    }
    close() {
      this.closed = true;
      this.onclose?.({ code: 1000 });
    }
    frame(frame) {
      this.onmessage?.({ data: JSON.stringify(frame) });
    }
  }
  globalThis.WebSocket = Socket;
  globalThis.window = new EventTarget();
  globalThis.document = new EventTarget();
  globalThis.document.hidden = false;
  let snapshots = 0,
    changes = 0;
  try {
    const dispose = subscribeThread(
      "ws://test/ws/thread",
      () => "token",
      (e) => {
        if (e.type === "work_changed") changes++;
      },
      () => snapshots++,
    );
    sockets[0].onopen();
    assert.deepEqual(sockets[0].auth, { type: "auth", access_token: "token" });
    sockets[0].frame({ type: "execution_status" });
    sockets[0].frame({ type: "work_changed" });
    assert.equal(snapshots, 1);
    assert.equal(changes, 1);
    for (const type of ["online", "focus", "aiive:resume"])
      globalThis.window.dispatchEvent(new Event(type));
    globalThis.document.dispatchEvent(new Event("visibilitychange"));
    assert.equal(snapshots, 5);
    assert.equal(sockets.length, 5);
    sockets.at(-1).close();
    await new Promise((resolve) => setTimeout(resolve, 1100));
    assert.equal(sockets.length, 6);
    sockets.at(-1).frame({ type: "execution_status" });
    assert.equal(snapshots, 6);
    dispose();
    const before = sockets.length;
    globalThis.window.dispatchEvent(new Event("online"));
    assert.equal(sockets.length, before);
    assert.ok(sockets.every((s) => s.closed));
  } finally {
    Object.assign(globalThis, original);
  }
});

test("changing Thread subscription leaves both production chat SSE streams running", async () => {
  const original = {
    fetch: globalThis.fetch,
    WebSocket: globalThis.WebSocket,
    window: globalThis.window,
    document: globalThis.document,
    localStorage: globalThis.localStorage,
  };
  class Socket {
    send() {}
    close() {
      this.onclose?.({ code: 1000 });
    }
  }
  globalThis.WebSocket = Socket;
  globalThis.window = new EventTarget();
  globalThis.document = new EventTarget();
  globalThis.localStorage = { getItem: () => "token" };
  const card = {
    card_type: "agent_task_created",
    title: "work",
    resource_refs: { task_id: "task" },
    payload_preview: {},
  };
  try {
    for (const platform of ["web", "android"]) {
      let stream,
        aborted = false;
      const encoder = new TextEncoder(),
        tokens = [],
        signal = new AbortController();
      globalThis.fetch = async (_url, options) => {
        options.signal.addEventListener("abort", () => {
          aborted = true;
        });
        return new Response(
          new ReadableStream({
            start(controller) {
              stream = controller;
            },
          }),
        );
      };
      const pending =
        platform === "web"
          ? webChat.sendMessageStream(
              "delegate",
              "thread",
              () => {},
              (token) => tokens.push(token),
              () => {},
              () => {},
              assert.fail,
              signal.signal,
            )
          : androidChat.streamMessage(
              "delegate",
              "thread",
              { onStarted() {}, onToken: (token) => tokens.push(token) },
              signal.signal,
            );
      await tick();
      stream.enqueue(
        encoder.encode(
          'event: started\ndata: {"thread_id":"thread","turn_record_id":"turn"}\n\nevent: token\ndata: {"text":"working"}\n\n',
        ),
      );
      await tick();
      const unsubscribe = androidChat.subscribeThreadEvents("thread", () => {});
      unsubscribe(); // Work page takes over Thread; only unmount/logout should abort a chat stream.
      const unsubscribeWork = subscribeThread(
        "wss://api.example/ws/thread",
        () => "token",
        () => {},
        () => {},
      );
      stream.enqueue(
        encoder.encode(
          `event: done\ndata: ${JSON.stringify({ thread_id: "thread", trace_id: "trace", reply: "delegated", action_cards: [card, card], pending_operations: [] })}\n\n`,
        ),
      );
      stream.close();
      const result = await pending;
      unsubscribeWork();
      assert.deepEqual(tokens, ["working"]);
      assert.equal(aborted, false);
      assert.equal(
        platform === "web"
          ? workCardsFrom(result.action_cards).length
          : result.workCards.length,
        1,
      );
    }
  } finally {
    Object.assign(globalThis, original);
  }
});

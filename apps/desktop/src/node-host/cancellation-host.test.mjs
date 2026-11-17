import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fork } from "node:child_process";
import test from "node:test";
import { stableStringify } from "./journal.mjs";

// Only the WebSocket transport is replaced. client.mjs runs real scope checks,
// subprocess execution, cancellation handling and the persistent Journal.
async function host(directory) {
  const wrapper = path.join(directory, "transport.mjs");
  await fs.writeFile(wrapper, `
    let socket;
    globalThis.WebSocket = class extends EventTarget {
      static OPEN = 1;
      readyState = 1;
      constructor() { super(); socket = this; queueMicrotask(() => this.dispatchEvent(new Event('open'))); }
      send(value) { process.send(JSON.parse(value)); }
      close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
    };
    process.on('message', value => socket.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(value)})));
    await import(${JSON.stringify(new URL("./client.mjs", import.meta.url).href)});
  `);
  const child = fork(wrapper, [], {
    env: { ...process.env, AIIVE_AUTH_ENABLED: "false", AIIVE_DESKTOP_NODE_ID: "test-node",
      AIIVE_DESKTOP_JOURNAL_PATH: path.join(directory, "journal.json") },
    stdio: ["ignore", "ignore", "inherit", "ipc"],
  });
  const messages = [];
  child.on("message", value => messages.push(value));
  async function wait(type, predicate = () => true) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      const index = messages.findIndex(value => value.type === type && predicate(value.data));
      if (index >= 0) return messages.splice(index, 1)[0].data;
      assert.equal(child.exitCode, null, "host exited before replying");
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    throw new Error(`host did not send ${type}`);
  }
  await wait("hello");
  return { child, wait, send: value => child.send(value), async stop() {
    if (child.exitCode !== null) return;
    await new Promise(resolve => { child.once("exit", resolve); child.kill(); });
  } };
}

test("real Node host stops a running process and preserves UNKNOWN across restart", { timeout: 30000 }, async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "aiive-cancel-host-"));
  let node;
  try {
    node = await host(directory);
    const params = { command: 'node -e "require(\'fs\').writeFileSync(\'started.txt\',\'1\');setTimeout(function(){},30000)"', cwd: directory };
    const request = {
      action_id: "action", request_id: "action", task_id: "task", run_id: "run",
      idempotency_key: "idem", arguments_hash: crypto.createHash("sha256").update(stableStringify(params)).digest("hex"),
      capability_id: "desktop_exec", params, preconditions: {}, fencing_token: "fence", timeout_seconds: 30,
      scope: { allowed_capabilities: ["desktop_exec"], allowed_roots: [directory], allow_secrets: false },
    };
    node.send({ type: "operation.request", data: request });
    await node.wait("operation.ack", value => value.status === "started");
    const marker = path.join(directory, "started.txt");
    const deadline = Date.now() + 10000;
    while (true) {
      try { await fs.stat(marker); break; } catch {
        if (Date.now() >= deadline) throw new Error("process did not start");
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    }
    const cancel = { action_id: "action", request_id: "action", fencing_token: "stale" };
    node.send({ type: "operation.cancel", data: cancel });
    assert.equal((await node.wait("operation.cancel.result")).status, "identity_mismatch");
    node.send({ type: "operation.cancel", data: { ...cancel, fencing_token: "fence" } });
    assert.equal((await node.wait("operation.cancel.result")).status, "requested");
    assert.equal((await node.wait("operation.result")).status, "unknown");
    const journal = JSON.parse(await fs.readFile(path.join(directory, "journal.json"), "utf8"));
    assert.equal(journal.entries[0].status, "unknown");
    assert.ok(journal.entries[0].cancel_requested_at);
    await node.stop();
    await fs.unlink(marker);
    node = await host(directory);
    node.send({ type: "operation.request", data: request });
    const replay = await node.wait("operation.result");
    assert.equal(replay.status, "unknown");
    assert.match(replay.error, /execution_unknown_in_journal/);
    await assert.rejects(fs.stat(marker), { code: "ENOENT" });
  } finally {
    if (node) await node.stop();
    await fs.rm(directory, { recursive: true, force: true });
  }
});

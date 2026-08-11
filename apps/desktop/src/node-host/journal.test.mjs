import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { DesktopActionJournal, InFlightActionCoordinator } from "./journal.mjs";
import {
  NodeResourceLeaseManager, assertPreconditions, assertResolvedPathScope, assertTaskScope,
} from "./locks.mjs";


test("journal persists terminal action and replays it after restart", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "aiive-journal-"));
  const file = path.join(directory, "journal.json");
  try {
    const identity = {
      action_id: "action-1", task_id: "task-1", idempotency_key: "idem-1",
      arguments_hash: "hash-1", capability_id: "desktop_fs_stat",
    };
    const journal = await new DesktopActionJournal(file).load();
    await journal.record(identity, "received");
    await journal.record(identity, "started");
    await journal.record(identity, "committed", { result: { size: 42 }, result_hash: "result-hash" });

    const restored = await new DesktopActionJournal(file).load();
    assert.equal(restored.get("action-1").status, "committed");
    assert.deepEqual(restored.get("action-1").result, { size: 42 });
    await assert.rejects(
      () => restored.record({ ...identity, idempotency_key: "different" }, "received"),
      /identity_conflict/,
    );
    await assert.rejects(
      () => restored.record({ ...identity, action_id: "action-2" }, "received"),
      /idempotency_conflict/,
    );
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});


test("concurrent duplicate action deliveries share exactly one execution", async () => {
  const coordinator = new InFlightActionCoordinator();
  const identity = {
    action_id: "action-1", task_id: "task-1", idempotency_key: "idem-1",
    arguments_hash: "hash-1", capability_id: "desktop_fs_write_text",
  };
  let executions = 0;
  let release;
  const callback = async () => {
    executions += 1;
    await new Promise((resolve) => { release = resolve; });
    return "committed";
  };
  const first = coordinator.run(identity, callback);
  await new Promise((resolve) => setImmediate(resolve));
  const duplicate = coordinator.run(identity, callback);
  await assert.rejects(
    () => coordinator.run({ ...identity, arguments_hash: "different" }, callback),
    /inflight_identity_conflict/,
  );
  release();

  const [firstResult, duplicateResult] = await Promise.all([first, duplicate]);
  assert.equal(executions, 1);
  assert.equal(firstResult.joined, false);
  assert.equal(duplicateResult.joined, true);
  assert.equal(duplicateResult.value, "committed");
});


test("node repeats path and capability scope enforcement", () => {
  assert.doesNotThrow(() => assertTaskScope({
    capability_id: "desktop_fs_read_text",
    params: { path: path.join(os.tmpdir(), "allowed", "note.txt") },
    scope: {
      allowed_capabilities: ["desktop_fs_read_text"],
      allowed_roots: [path.join(os.tmpdir(), "allowed")],
    },
  }));
  assert.throws(() => assertTaskScope({
    capability_id: "desktop_fs_read_text",
    params: { path: path.join(os.tmpdir(), "outside.txt") },
    scope: {
      allowed_capabilities: ["desktop_fs_read_text"],
      allowed_roots: [path.join(os.tmpdir(), "allowed")],
    },
  }), /scope_path_denied/);
});


test("resolved scope rejects a symlink escape", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "aiive-scope-"));
  const allowed = path.join(directory, "allowed");
  const outside = path.join(directory, "outside");
  await fs.mkdir(allowed);
  await fs.mkdir(outside);
  await fs.writeFile(path.join(outside, "secret.txt"), "secret");
  await fs.symlink(outside, path.join(allowed, "link"));
  const request = {
    capability_id: "desktop_fs_read_text",
    params: { path: path.join(allowed, "link", "secret.txt") },
    scope: { allowed_capabilities: ["desktop_fs_read_text"], allowed_roots: [allowed] },
  };
  try {
    assertTaskScope(request);
    await assert.rejects(() => assertResolvedPathScope(request, fs), /scope_resolved_path_denied/);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});


test("move source preconditions validate stable file identity and metadata", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "aiive-precondition-"));
  const source = path.join(directory, "source.txt");
  await fs.writeFile(source, "source");
  const stat = await fs.stat(source, { bigint: true });
  const request = {
    params: { source },
    preconditions: {
      exists: true,
      size: String(stat.size),
      mtime_ns: String(stat.mtimeNs),
      file_id: `${stat.dev}:${stat.ino}`,
    },
  };
  try {
    await assert.doesNotReject(() => assertPreconditions(request, fs, crypto));
    await assert.rejects(
      () => assertPreconditions({ ...request, preconditions: { ...request.preconditions, size: "999" } }, fs, crypto),
      /precondition_size_changed/,
    );
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});


test("foreground/path lease prevents concurrent conflicting actions", async () => {
  const leases = new NodeResourceLeaseManager();
  const request = {
    action_id: "action-1", capability_id: "desktop_fs_write_text",
    params: { path: path.join(os.tmpdir(), "shared.txt") },
  };
  let release;
  const held = leases.withLease(request, () => new Promise((resolve) => { release = resolve; }));
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(
    () => leases.withLease({ ...request, action_id: "action-2" }, async () => {}),
    /resource_busy/,
  );
  release();
  await held;
});


test("readonly actions do not take exclusive path leases", async () => {
  const leases = new NodeResourceLeaseManager();
  const request = {
    action_id: "read-1", capability_id: "desktop_fs_read_text",
    params: { path: path.join(os.tmpdir(), "shared-read.txt") },
  };
  let release;
  const held = leases.withLease(request, () => new Promise((resolve) => { release = resolve; }));
  await new Promise((resolve) => setImmediate(resolve));
  await leases.withLease({ ...request, action_id: "read-2" }, async () => {});
  release();
  await held;
});

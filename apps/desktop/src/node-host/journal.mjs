import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";


export function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}


function sameIdentity(left, right) {
  return ["action_id", "task_id", "idempotency_key", "arguments_hash", "capability_id"]
    .every((field) => left?.[field] === right?.[field]);
}


/** Coalesces concurrent deliveries of one Action before the persistent journal exists. */
export class InFlightActionCoordinator {
  constructor() {
    this.actions = new Map();
  }

  async run(identity, callback) {
    const existing = this.actions.get(identity.action_id);
    if (existing) {
      if (!sameIdentity(existing.identity, identity)) {
        throw new Error("desktop_inflight_identity_conflict");
      }
      return { joined: true, value: await existing.promise };
    }
    // Deferring callback to a microtask guarantees the Map entry exists before
    // execution reaches its first asynchronous journal/scope operation.
    const promise = Promise.resolve().then(callback);
    this.actions.set(identity.action_id, { identity: { ...identity }, promise });
    try {
      return { joined: false, value: await promise };
    } finally {
      if (this.actions.get(identity.action_id)?.promise === promise) {
        this.actions.delete(identity.action_id);
      }
    }
  }
}


export class DesktopActionJournal {
  constructor(filePath = process.env.AIIVE_DESKTOP_JOURNAL_PATH || path.join(os.homedir(), ".aiive", "action-journal.json")) {
    this.filePath = path.resolve(filePath);
    this.entriesById = new Map();
    this.writeQueue = Promise.resolve();
  }

  async load() {
    try {
      const payload = JSON.parse(await fs.readFile(this.filePath, "utf8"));
      for (const entry of Array.isArray(payload?.entries) ? payload.entries : []) {
        if (entry?.action_id) this.entriesById.set(entry.action_id, entry);
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    return this;
  }

  get(actionId) {
    return this.entriesById.get(actionId);
  }

  entries(actionIds) {
    const filter = new Set(Array.isArray(actionIds) ? actionIds : []);
    return [...this.entriesById.values()].filter((entry) => filter.size === 0 || filter.has(entry.action_id));
  }

  async record(identity, status, patch = {}) {
    const existing = this.entriesById.get(identity.action_id) || {};
    if (existing.idempotency_key && (
      existing.idempotency_key !== identity.idempotency_key
      || existing.arguments_hash !== identity.arguments_hash
      || existing.task_id !== identity.task_id
      || existing.capability_id !== identity.capability_id
    )) {
      throw new Error("desktop_journal_identity_conflict");
    }
    for (const entry of this.entriesById.values()) {
      if (entry.action_id !== identity.action_id && entry.idempotency_key === identity.idempotency_key) {
        throw new Error("desktop_journal_idempotency_conflict");
      }
    }
    const entry = {
      ...existing,
      action_id: identity.action_id,
      task_id: identity.task_id,
      idempotency_key: identity.idempotency_key,
      arguments_hash: identity.arguments_hash,
      capability_id: identity.capability_id,
      status,
      ...patch,
      updated_at: new Date().toISOString(),
    };
    this.entriesById.set(identity.action_id, entry);
    await this.persist();
    return entry;
  }

  async persist() {
    this.writeQueue = this.writeQueue.then(async () => {
      await fs.mkdir(path.dirname(this.filePath), { recursive: true });
      const entries = [...this.entriesById.values()]
        .sort((a, b) => String(a.updated_at).localeCompare(String(b.updated_at)));
      const temporary = `${this.filePath}.${process.pid}.tmp`;
      const handle = await fs.open(temporary, "w", 0o600);
      try {
        await handle.writeFile(JSON.stringify({ version: 2, entries }));
        await handle.sync();
      } finally {
        await handle.close();
      }
      await fs.rename(temporary, this.filePath);
      try {
        const directory = await fs.open(path.dirname(this.filePath), "r");
        try { await directory.sync(); } finally { await directory.close(); }
      } catch { /* 某些 Windows 文件系统不允许打开目录；原子 rename 仍保留。 */ }
    });
    return this.writeQueue;
  }
}

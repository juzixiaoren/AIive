import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { DesktopActionJournal, InFlightActionCoordinator, stableStringify } from "./journal.mjs";
import {
  NodeResourceLeaseManager,
  assertPreconditions,
  assertResolvedPathScope,
  assertTaskScope,
} from "./locks.mjs";

const nodeId = process.env.AIIVE_DESKTOP_NODE_ID;
const nodeName = process.env.AIIVE_DESKTOP_NODE_NAME || os.hostname();
const backendUrl = (process.env.AIIVE_DESKTOP_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const appVersion = process.env.AIIVE_DESKTOP_APP_VERSION || "0.1.0";
const authToken = process.env.AIIVE_DESKTOP_NODE_AUTH_TOKEN || "aiive-desktop-dev-token";
const protocolVersion = 2;

if (!nodeId) throw new Error("AIIVE_DESKTOP_NODE_ID is required");

const capabilities = [
  {
    name: "desktop_system_info",
    description: "读取当前桌面电脑的操作系统、主机名、CPU、内存和用户目录信息。",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "desktop_fs_stat",
    description: "读取桌面电脑上任意本地路径的文件类型、大小、时间和规范化路径。",
    input_schema: {
      type: "object",
      properties: { path: { type: "string", minLength: 1, description: "本地文件或目录路径" } },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_list",
    description: "列出桌面电脑上的本地目录，可限制返回条目数。",
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1, description: "本地目录路径" },
        limit: { type: "integer", minimum: 1, maximum: 2000, default: 200 },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_read_text",
    description: "按字节偏移分段读取桌面电脑上的 UTF-8 文本文件，单次最多 262144 字节。",
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1, description: "本地文本文件路径" },
        offset: { type: "integer", minimum: 0, default: 0 },
        max_bytes: { type: "integer", minimum: 1, maximum: 262144, default: 65536 },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_read_binary",
    description: "按字节偏移分段读取桌面电脑上的任意二进制文件，以 Base64 返回，单次最多 1048576 字节。",
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1, description: "本地文件路径" },
        offset: { type: "integer", minimum: 0, default: 0 },
        max_bytes: { type: "integer", minimum: 1, maximum: 1048576, default: 262144 },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_write_text",
    description: "在桌面电脑上原子新建或覆盖 UTF-8 文本文件，可用 SHA-256 防止覆盖并发修改。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        content: { type: "string" },
        expected_sha256: { type: "string", description: "可选；目标当前内容必须匹配该哈希" },
        create_parents: { type: "boolean", default: true },
      },
      required: ["path", "content"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_write_binary",
    description: "在桌面电脑上原子新建或覆盖任意二进制文件，内容使用 Base64，并可用 SHA-256 防止覆盖并发修改。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        content_base64: { type: "string" },
        expected_sha256: { type: "string" },
        create_parents: { type: "boolean", default: true },
      },
      required: ["path", "content_base64"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_edit_text",
    description: "在桌面电脑上的文本文件中精确替换内容，并通过可选 SHA-256 防止覆盖并发修改。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        old_text: { type: "string", minLength: 1 },
        new_text: { type: "string" },
        replace_all: { type: "boolean", default: false },
        expected_sha256: { type: "string" },
      },
      required: ["path", "old_text", "new_text"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_mkdir",
    description: "在桌面电脑上创建目录，默认递归创建父目录。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        recursive: { type: "boolean", default: true },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_copy",
    description: "在桌面电脑上复制文件或目录。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        source: { type: "string", minLength: 1 },
        destination: { type: "string", minLength: 1 },
        overwrite: { type: "boolean", default: false },
      },
      required: ["source", "destination"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_move",
    description: "在桌面电脑的同一磁盘内移动或重命名文件、目录。",
    risk_level: "medium",
    writes_external_world: true,
    input_schema: {
      type: "object",
      properties: {
        source: { type: "string", minLength: 1 },
        destination: { type: "string", minLength: 1 },
      },
      required: ["source", "destination"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_fs_delete",
    description: "删除桌面电脑上的普通文件或目录。每次调用必须由用户确认；系统关键路径永远拒绝。",
    risk_level: "high",
    requires_confirmation: true,
    writes_external_world: true,
    can_delete: true,
    input_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        mode: { type: "string", enum: ["trash", "permanent"], default: "trash" },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    name: "desktop_exec",
    description: "在桌面电脑上通过系统 Shell 执行命令，返回退出码、stdout 和 stderr。每次调用必须由用户确认。",
    risk_level: "high",
    requires_confirmation: true,
    writes_external_world: true,
    timeout_seconds: 660,
    input_schema: {
      type: "object",
      properties: {
        command: { type: "string", minLength: 1 },
        cwd: { type: "string", description: "工作目录，默认用户主目录" },
        timeout_seconds: { type: "integer", minimum: 1, maximum: 600, default: 120 },
        env: { type: "object", description: "附加环境变量" },
      },
      required: ["command"],
      additionalProperties: false,
    },
  },
];

function absolute(input) {
  return path.resolve(String(input));
}

async function systemInfo() {
  return {
    hostname: os.hostname(),
    platform: process.platform,
    release: os.release(),
    arch: process.arch,
    cpus: os.cpus().map((cpu) => cpu.model),
    total_memory: os.totalmem(),
    free_memory: os.freemem(),
    home_directory: os.homedir(),
    temp_directory: os.tmpdir(),
  };
}

async function statFile(params) {
  const requestedPath = absolute(params.path);
  const stat = await fs.lstat(requestedPath);
  let realPath = requestedPath;
  try { realPath = await fs.realpath(requestedPath); } catch { /* 保留规范化输入 */ }
  return {
    path: requestedPath,
    real_path: realPath,
    type: stat.isFile() ? "file" : stat.isDirectory() ? "directory" : stat.isSymbolicLink() ? "symlink" : "other",
    size: stat.size,
    mode: stat.mode,
    created_at: stat.birthtime.toISOString(),
    modified_at: stat.mtime.toISOString(),
  };
}

async function listDirectory(params) {
  const requestedPath = absolute(params.path);
  const limit = Math.max(1, Math.min(2000, Number(params.limit ?? 200)));
  const entries = await fs.readdir(requestedPath, { withFileTypes: true });
  return {
    path: requestedPath,
    entries: entries.slice(0, limit).map((entry) => ({
      name: entry.name,
      type: entry.isFile() ? "file" : entry.isDirectory() ? "directory" : entry.isSymbolicLink() ? "symlink" : "other",
    })),
    truncated: entries.length > limit,
    total_entries: entries.length,
  };
}

async function readText(params) {
  const requestedPath = absolute(params.path);
  const offset = Math.max(0, Number(params.offset ?? 0));
  const maxBytes = Math.max(1, Math.min(262144, Number(params.max_bytes ?? 65536)));
  const handle = await fs.open(requestedPath, "r");
  try {
    const stat = await handle.stat();
    const available = Math.max(0, stat.size - offset);
    const length = Math.min(maxBytes, available);
    const buffer = Buffer.alloc(length);
    const { bytesRead } = await handle.read(buffer, 0, length, offset);
    return {
      path: requestedPath,
      offset,
      bytes_read: bytesRead,
      total_bytes: stat.size,
      truncated: offset + bytesRead < stat.size,
      content: buffer.subarray(0, bytesRead).toString("utf8"),
    };
  } finally {
    await handle.close();
  }
}

async function readBinary(params) {
  const requestedPath = absolute(params.path);
  const offset = Math.max(0, Number(params.offset ?? 0));
  const maxBytes = Math.max(1, Math.min(1048576, Number(params.max_bytes ?? 262144)));
  const handle = await fs.open(requestedPath, "r");
  try {
    const stat = await handle.stat();
    const length = Math.min(maxBytes, Math.max(0, stat.size - offset));
    const buffer = Buffer.alloc(length);
    const { bytesRead } = await handle.read(buffer, 0, length, offset);
    return {
      path: requestedPath,
      offset,
      bytes_read: bytesRead,
      total_bytes: stat.size,
      truncated: offset + bytesRead < stat.size,
      content_base64: buffer.subarray(0, bytesRead).toString("base64"),
      chunk_sha256: crypto.createHash("sha256").update(buffer.subarray(0, bytesRead)).digest("hex"),
    };
  } finally {
    await handle.close();
  }
}

async function sha256File(filePath) {
  const content = await fs.readFile(filePath);
  return crypto.createHash("sha256").update(content).digest("hex");
}

async function assertExpectedHash(filePath, expected) {
  if (!expected) return;
  const current = await sha256File(filePath);
  if (current.toLowerCase() !== String(expected).toLowerCase()) {
    throw new Error(`file_changed: expected ${expected}, actual ${current}`);
  }
}

async function atomicWrite(filePath, content, createParents = true) {
  const destination = absolute(filePath);
  if (createParents) await fs.mkdir(path.dirname(destination), { recursive: true });
  let mode = 0o600;
  try { mode = (await fs.stat(destination)).mode; } catch { /* 新文件使用私有默认权限 */ }
  const tempPath = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.aiive-${crypto.randomUUID()}.tmp`,
  );
  const bytes = Buffer.isBuffer(content) ? content : Buffer.from(String(content), "utf8");
  await fs.writeFile(tempPath, bytes, { mode });
  try {
    await fs.rename(tempPath, destination);
  } catch (error) {
    await fs.rm(tempPath, { force: true });
    throw error;
  }
  return { path: destination, bytes_written: bytes.length, sha256: await sha256File(destination) };
}

async function writeText(params) {
  const destination = absolute(params.path);
  await assertExpectedHash(destination, params.expected_sha256);
  return atomicWrite(destination, params.content, params.create_parents !== false);
}

async function writeBinary(params) {
  const destination = absolute(params.path);
  await assertExpectedHash(destination, params.expected_sha256);
  const encoded = String(params.content_base64 || "");
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
    throw new Error("invalid_base64");
  }
  return atomicWrite(destination, Buffer.from(encoded, "base64"), params.create_parents !== false);
}

async function editText(params) {
  const destination = absolute(params.path);
  await assertExpectedHash(destination, params.expected_sha256);
  const original = await fs.readFile(destination, "utf8");
  if (!original.includes(params.old_text)) throw new Error("old_text_not_found");
  const updated = params.replace_all
    ? original.split(params.old_text).join(params.new_text)
    : original.replace(params.old_text, params.new_text);
  const result = await atomicWrite(destination, updated, false);
  return { ...result, replacements: params.replace_all ? original.split(params.old_text).length - 1 : 1 };
}

async function makeDirectory(params) {
  const destination = absolute(params.path);
  await fs.mkdir(destination, { recursive: params.recursive !== false });
  return { path: destination, created: true };
}

async function copyPath(params) {
  const source = absolute(params.source);
  const destination = absolute(params.destination);
  await fs.cp(source, destination, {
    recursive: true,
    force: Boolean(params.overwrite),
    errorOnExist: !params.overwrite,
    preserveTimestamps: true,
  });
  return { source, destination, copied: true };
}

async function movePath(params) {
  const source = absolute(params.source);
  const destination = absolute(params.destination);
  try {
    await fs.lstat(destination);
    throw new Error("destination_exists");
  } catch (error) {
    if (error?.message === "destination_exists") throw error;
    if (error?.code !== "ENOENT") throw error;
  }
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.rename(source, destination);
  return { source, destination, moved: true };
}

function comparePath(value) {
  const normalized = path.resolve(value).replace(/[\\/]+$/, "") || path.parse(path.resolve(value)).root;
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function isWithin(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function protectedDeleteRoots() {
  if (process.platform === "win32") {
    return [
      process.env.SystemRoot || process.env.WINDIR || "C:\\Windows",
      process.env.ProgramFiles || "C:\\Program Files",
      process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
      process.env.ProgramData ? path.join(process.env.ProgramData, "Microsoft") : "C:\\ProgramData\\Microsoft",
    ];
  }
  if (process.platform === "darwin") {
    return ["/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/private/var/db"];
  }
  return ["/boot", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var"];
}

async function validateDeleteTarget(input) {
  const requested = absolute(input);
  const stat = await fs.lstat(requested);
  const resolved = stat.isSymbolicLink() ? requested : await fs.realpath(requested);
  const normalized = comparePath(resolved);
  const parsedRoot = comparePath(path.parse(resolved).root);
  const home = comparePath(os.homedir());
  if (normalized === parsedRoot || isWithin(home, normalized)) {
    throw new Error("protected_delete_path");
  }
  for (const protectedRoot of protectedDeleteRoots()) {
    const root = comparePath(protectedRoot);
    if (isWithin(normalized, root) || isWithin(root, normalized)) {
      throw new Error(`protected_delete_path: ${protectedRoot}`);
    }
  }
  return { requested, resolved, stat };
}

async function deletePath(params) {
  const { requested, resolved, stat } = await validateDeleteTarget(params.path);
  const mode = params.mode || "trash";
  if (mode === "trash") {
    const trashRoot = path.join(os.homedir(), ".aiive", "trash");
    await fs.mkdir(trashRoot, { recursive: true });
    const destination = path.join(
      trashRoot,
      `${Date.now()}-${crypto.randomUUID()}-${path.basename(resolved)}`,
    );
    try {
      await fs.rename(resolved, destination);
    } catch (error) {
      if (error?.code !== "EXDEV") throw error;
      await fs.cp(resolved, destination, { recursive: true, errorOnExist: true });
      await fs.rm(resolved, { recursive: stat.isDirectory(), force: false });
    }
    return { path: requested, resolved_path: resolved, mode, trash_path: destination };
  }
  if (mode !== "permanent") throw new Error("invalid_delete_mode");
  await fs.rm(resolved, { recursive: stat.isDirectory(), force: false });
  return { path: requested, resolved_path: resolved, mode };
}

function assertCommandDoesNotTargetCriticalDelete(command) {
  const raw = String(command);
  const normalized = raw.toLowerCase().replaceAll("/", "\\");
  const destructive = /(^|[\s;&|])(rm|rmdir|del|erase|remove-item|rd|format|mkfs|diskpart)([\s;&|]|$)/i.test(raw);
  if (!destructive) return;

  if (process.platform === "win32") {
    const criticalTokens = [
      "c:\\windows",
      "\\system32",
      "c:\\program files",
      "c:\\programdata\\microsoft",
      "\\boot",
      "\\recovery",
    ];
    const driveRootTarget = /(?:^|[\s'"=])(?:[a-z]:\\)(?:\*|\.{0,2})?(?=$|[\s'";&|])/i.test(normalized);
    const homeAliasTarget = /(?:^|[\s'"=])(?:%userprofile%|%homepath%)(?:\\\*)?(?=$|[\s'";&|])/i.test(normalized);
    if (driveRootTarget || homeAliasTarget || criticalTokens.some((token) => normalized.includes(token))) {
      throw new Error("protected_delete_command");
    }
    return;
  }

  const criticalUnixTarget = /(?:^|[\s'"=])\/(?:system|library|applications|private|usr|bin|sbin|etc|boot|proc|sys|dev|lib|lib64|var)(?:\/|$|[\s'";&|])/i.test(raw);
  const rootTarget = /(?:^|[\s'"=])\/(?:\*|\.{0,2})?(?=$|[\s'";&|])/i.test(raw);
  const homeAliasTarget = /(?:^|[\s'"=])(?:~|\$home|\$\{home\})(?:\/\*)?(?=$|[\s'";&|])/i.test(raw);
  if (criticalUnixTarget || rootTarget || homeAliasTarget) {
    throw new Error("protected_delete_command");
  }
}

async function executeCommand(params, executionScope = {}) {
  const command = String(params.command);
  assertCommandDoesNotTargetCriticalDelete(command);
  const cwd = absolute(params.cwd || os.homedir());
  const timeoutSeconds = Math.max(1, Math.min(600, Number(params.timeout_seconds ?? 120)));
  const extraEnv = params.env && typeof params.env === "object"
    ? Object.fromEntries(Object.entries(params.env).map(([key, value]) => [key, String(value)]))
    : {};
  const inheritedEnv = executionScope.allow_secrets
    ? process.env
    : Object.fromEntries(Object.entries(process.env).filter(([key]) => [
      "PATH", "Path", "PATHEXT", "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec",
      "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
    ].includes(key)));
  return new Promise((resolve, reject) => {
    const child = spawn(command, {
      cwd,
      env: { ...inheritedEnv, ...extraEnv, AIIVE_SHELL: "desktop_exec" },
      shell: true,
      windowsHide: true,
    });
    const stdout = [];
    const stderr = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    const outputLimit = 1024 * 1024;
    child.stdout?.on("data", (chunk) => {
      if (stdoutBytes < outputLimit) stdout.push(chunk.subarray(0, outputLimit - stdoutBytes));
      stdoutBytes += chunk.length;
    });
    child.stderr?.on("data", (chunk) => {
      if (stderrBytes < outputLimit) stderr.push(chunk.subarray(0, outputLimit - stderrBytes));
      stderrBytes += chunk.length;
    });
    const timer = setTimeout(() => child.kill("SIGTERM"), timeoutSeconds * 1000);
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      resolve({
        command,
        cwd,
        exit_code: code,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
        stdout_truncated: stdoutBytes > outputLimit,
        stderr_truncated: stderrBytes > outputLimit,
      });
    });
  });
}

const handlers = new Map([
  ["desktop_system_info", systemInfo],
  ["desktop_fs_stat", statFile],
  ["desktop_fs_list", listDirectory],
  ["desktop_fs_read_text", readText],
  ["desktop_fs_read_binary", readBinary],
  ["desktop_fs_write_text", writeText],
  ["desktop_fs_write_binary", writeBinary],
  ["desktop_fs_edit_text", editText],
  ["desktop_fs_mkdir", makeDirectory],
  ["desktop_fs_copy", copyPath],
  ["desktop_fs_move", movePath],
  ["desktop_fs_delete", deletePath],
  ["desktop_exec", executeCommand],
]);

function websocketUrl() {
  const url = new URL(backendUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/ws/desktop/${encodeURIComponent(nodeId)}`;
  url.search = "";
  return url.toString();
}

const journal = await new DesktopActionJournal().load();
const resourceLeases = new NodeResourceLeaseManager();
const inFlightActions = new InFlightActionCoordinator();
let retryMs = 1000;

function sendJson(socket, payload) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

function resultEnvelope(request, entry, replay = false) {
  return {
    request_id: request.request_id,
    action_id: request.action_id,
    idempotency_key: request.idempotency_key,
    arguments_hash: request.arguments_hash,
    status: entry.status,
    result_hash: entry.result_hash || "",
    ok: entry.status === "committed",
    result: entry.result,
    error: entry.error || "",
    idempotent_replay: replay,
  };
}

async function handleOperation(socket, request) {
  const handler = handlers.get(String(request.capability_id || ""));
  if (!handler) throw new Error("unknown_desktop_capability");
  if (!request.action_id || !request.task_id || !request.idempotency_key || !request.arguments_hash) {
    throw new Error("invalid_action_identity");
  }
  const actualHash = crypto.createHash("sha256").update(stableStringify(request.params || {})).digest("hex");
  if (actualHash !== request.arguments_hash) throw new Error("arguments_hash_mismatch");
  const identity = {
    action_id: request.action_id,
    task_id: request.task_id,
    idempotency_key: request.idempotency_key,
    arguments_hash: request.arguments_hash,
    capability_id: request.capability_id,
  };
  const coordinated = await inFlightActions.run(identity, async () => {
    const prior = journal.get(request.action_id);
    if (prior) {
      if (
        prior.idempotency_key !== request.idempotency_key
        || prior.arguments_hash !== request.arguments_hash
        || prior.task_id !== request.task_id
        || prior.capability_id !== request.capability_id
      ) {
        throw new Error("desktop_journal_identity_conflict");
      }
      if (["committed", "failed"].includes(prior.status)) {
        return { entry: prior, replay: true };
      }
      throw new Error("execution_unknown_in_journal");
    }
    assertTaskScope(request);
    await assertResolvedPathScope(request, fs);
    await assertPreconditions(request, fs, crypto);
    await journal.record(identity, "received");
    sendJson(socket, {
      type: "operation.ack", protocol_version: protocolVersion,
      data: { request_id: request.request_id, action_id: request.action_id, status: "received" },
    });
    await journal.record(identity, "started", { started_at: new Date().toISOString() });
    sendJson(socket, {
      type: "operation.ack", protocol_version: protocolVersion,
      data: { request_id: request.request_id, action_id: request.action_id, status: "started" },
    });
    try {
      const result = await resourceLeases.withLease(request, async () => {
        assertTaskScope(request);
        await assertResolvedPathScope(request, fs);
        await assertPreconditions(request, fs, crypto);
        return handler(
          request.params || {},
          { allow_secrets: Boolean(request.scope?.allow_secrets) },
        );
      });
      const resultHash = crypto.createHash("sha256").update(stableStringify(result)).digest("hex");
      const entry = await journal.record(identity, "committed", {
        result, result_hash: resultHash, completed_at: new Date().toISOString(),
      });
      return { entry, replay: false };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const entry = await journal.record(identity, "failed", {
        error: message, completed_at: new Date().toISOString(),
      });
      return { entry, replay: false };
    }
  });
  sendJson(socket, {
    type: "operation.result",
    protocol_version: protocolVersion,
    data: resultEnvelope(
      request,
      coordinated.value.entry,
      coordinated.joined || coordinated.value.replay,
    ),
  });
}

function connect() {
  const socket = new WebSocket(websocketUrl());
  let heartbeat;
  socket.addEventListener("open", () => {
    retryMs = 1000;
    sendJson(socket, {
      type: "hello", protocol_version: protocolVersion,
      data: {
        node_id: nodeId, name: nodeName, platform: process.platform, arch: process.arch,
        app_version: appVersion, protocol_version: protocolVersion, auth_token: authToken, capabilities,
      },
    });
    heartbeat = setInterval(() => {
      sendJson(socket, { type: "heartbeat", protocol_version: protocolVersion, data: {} });
    }, 15000);
  });
  socket.addEventListener("message", async (event) => {
    let message;
    try { message = JSON.parse(String(event.data)); } catch { return; }
    if (message?.type === "journal.query") {
      const actionIds = Array.isArray(message.data?.action_ids) ? message.data.action_ids : [];
      sendJson(socket, {
        type: "journal.snapshot", protocol_version: protocolVersion,
        data: {
          entries: journal.entries(actionIds),
          missing_action_ids: actionIds.filter((actionId) => !journal.get(actionId)),
        },
      });
      return;
    }
    if (message?.type !== "operation.request") return;
    const request = message.data || {};
    try {
      await handleOperation(socket, request);
    } catch (error) {
      const messageText = error instanceof Error ? error.message : String(error);
      sendJson(socket, {
        type: "operation.result", protocol_version: protocolVersion,
        data: {
          request_id: request.request_id || request.action_id || crypto.randomUUID(),
          action_id: request.action_id || "",
          idempotency_key: request.idempotency_key || "",
          arguments_hash: request.arguments_hash || "",
          status: messageText.includes("unknown") ? "unknown" : "failed",
          ok: false,
          error: messageText,
        },
      });
    }
  });
  socket.addEventListener("close", () => {
    clearInterval(heartbeat);
    setTimeout(connect, retryMs);
    retryMs = Math.min(30000, retryMs * 2);
  });
  socket.addEventListener("error", () => socket.close());
}

connect();

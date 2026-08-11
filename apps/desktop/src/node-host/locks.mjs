import path from "node:path";


const PATH_FIELDS = new Set(["path", "source", "destination", "cwd", "file_path", "target_file"]);
const READ_ONLY_CAPABILITIES = new Set([
  "desktop_system_info", "desktop_fs_stat", "desktop_fs_list",
  "desktop_fs_read_text", "desktop_fs_read_binary",
]);

function normalized(value) {
  const resolved = path.resolve(String(value));
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function within(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export function assertTaskScope(request) {
  const scope = request.scope && typeof request.scope === "object" ? request.scope : {};
  const capabilities = new Set(Array.isArray(scope.allowed_capabilities) ? scope.allowed_capabilities : []);
  if (!capabilities.has(request.capability_id)) throw new Error("scope_capability_denied");
  const roots = (Array.isArray(scope.allowed_roots) ? scope.allowed_roots : []).map(normalized);
  for (const [name, value] of Object.entries(request.params || {})) {
    if (!PATH_FIELDS.has(name) || typeof value !== "string" || !value) continue;
    const candidate = normalized(value);
    if (roots.length === 0 || !roots.some((root) => within(candidate, root))) {
      throw new Error(`scope_path_denied:${name}`);
    }
  }
  if (request.capability_id === "desktop_exec") {
    if (!request.params?.cwd) throw new Error("scope_exec_cwd_required");
    if (request.params?.env && !scope.allow_secrets) throw new Error("scope_secret_environment_denied");
  }
}

export async function assertResolvedPathScope(request, fs) {
  const scope = request.scope && typeof request.scope === "object" ? request.scope : {};
  const roots = [];
  for (const value of Array.isArray(scope.allowed_roots) ? scope.allowed_roots : []) {
    const lexical = normalized(value);
    try { roots.push(normalized(await fs.realpath(lexical))); }
    catch { roots.push(lexical); }
  }
  for (const [name, value] of Object.entries(request.params || {})) {
    if (!PATH_FIELDS.has(name) || typeof value !== "string" || !value) continue;
    const lexical = normalized(value);
    let resolved;
    try { resolved = normalized(await fs.realpath(lexical)); }
    catch (error) {
      if (error?.code !== "ENOENT") throw error;
      const parent = path.dirname(lexical);
      try { resolved = normalized(path.join(await fs.realpath(parent), path.basename(lexical))); }
      catch { resolved = lexical; }
    }
    if (roots.length === 0 || !roots.some((root) => within(resolved, root))) {
      throw new Error(`scope_resolved_path_denied:${name}`);
    }
  }
}

export async function assertPreconditions(request, fs, crypto) {
  const preconditions = request.preconditions && typeof request.preconditions === "object"
    ? request.preconditions : {};
  const rawTarget = typeof request.params?.path === "string"
    ? request.params.path
    : typeof request.params?.source === "string" ? request.params.source : null;
  const target = rawTarget ? path.resolve(rawTarget) : null;
  if (!target) return;
  let exists = true;
  try { await fs.lstat(target); } catch (error) {
    if (error?.code === "ENOENT") exists = false;
    else throw error;
  }
  if (preconditions.exists === true && !exists) throw new Error("precondition_missing_path");
  if (preconditions.exists === false && exists) throw new Error("precondition_path_already_exists");
  if (exists) {
    const stat = await fs.stat(target, { bigint: true });
    if (preconditions.size !== undefined) {
      let expected;
      try { expected = BigInt(preconditions.size); }
      catch { throw new Error("precondition_size_invalid"); }
      if (stat.size !== expected) throw new Error("precondition_size_changed");
    }
    if (preconditions.mtime_ns !== undefined) {
      let expected;
      try { expected = BigInt(preconditions.mtime_ns); }
      catch { throw new Error("precondition_mtime_invalid"); }
      if (stat.mtimeNs !== expected) throw new Error("precondition_mtime_changed");
    }
    if (
      preconditions.file_id !== undefined
      && String(preconditions.file_id) !== `${stat.dev}:${stat.ino}`
    ) {
      throw new Error("precondition_file_id_changed");
    }
  }
  const expected = preconditions.sha256 || request.params.expected_sha256;
  if (expected && exists) {
    const content = await fs.readFile(target);
    const actual = crypto.createHash("sha256").update(content).digest("hex");
    if (actual.toLowerCase() !== String(expected).toLowerCase()) {
      throw new Error("precondition_sha256_changed");
    }
  }
}

export class NodeResourceLeaseManager {
  constructor() {
    this.owners = new Map();
  }

  keys(request) {
    const keys = [];
    if (!READ_ONLY_CAPABILITIES.has(request.capability_id)) {
      for (const [name, value] of Object.entries(request.params || {})) {
        if (PATH_FIELDS.has(name) && typeof value === "string" && value) keys.push(`path:${normalized(value)}`);
      }
    }
    if (/(gui|foreground|screen|keyboard|mouse)/.test(request.capability_id)) keys.push("gui:foreground");
    return [...new Set(keys)].sort();
  }

  async withLease(request, callback) {
    const keys = this.keys(request);
    for (const key of keys) {
      const owner = this.owners.get(key);
      if (owner && owner !== request.action_id) throw new Error(`resource_busy:${key}`);
    }
    for (const key of keys) this.owners.set(key, request.action_id);
    try { return await callback(); }
    finally {
      for (const key of keys) if (this.owners.get(key) === request.action_id) this.owners.delete(key);
    }
  }
}

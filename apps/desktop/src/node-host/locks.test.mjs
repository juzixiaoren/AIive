import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { assertTaskScope } from "./locks.mjs";

test("unified capability policy retains path and environment restrictions", () => {
  const root = path.resolve("workspace");
  const request = { capability_id: "desktop_exec", scope: { capability_policy: "unified", allowed_capabilities: [], allowed_roots: [root] }, params: { cwd: root } };
  assert.doesNotThrow(() => assertTaskScope(request));
  assert.throws(() => assertTaskScope({ ...request, params: { cwd: path.dirname(root) } }), /scope_path_denied/);
  assert.throws(() => assertTaskScope({ ...request, params: { cwd: root, env: { TOKEN: "x" } } }), /scope_secret_environment_denied/);
  assert.throws(() => assertTaskScope({ ...request, params: {} }), /scope_exec_cwd_required/);
  for (const policy of ["restricted", "invalid", null]) {
    assert.throws(() => assertTaskScope({ ...request, scope: { ...request.scope, capability_policy: policy } }), /scope_capability_denied/);
  }
});

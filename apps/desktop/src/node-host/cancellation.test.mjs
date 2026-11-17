import assert from "node:assert/strict";
import test from "node:test";
import { ActionCancellation } from "./cancellation.mjs";

const identity = { action_id: "action", request_id: "request", fencing_token: "fence", capability_id: "desktop_exec" };

test("cancellation binds all execution identities and never confirms a stop", () => {
  const control = new ActionCancellation();
  const entry = control.start(identity);
  assert.equal(control.request({ ...identity, fencing_token: "stale" }).status, "identity_mismatch");
  assert.equal(entry.controller.signal.aborted, false);
  assert.equal(control.request(identity).status, "requested");
  assert.equal(entry.controller.signal.aborted, true);
  control.finish(entry);
  assert.equal(control.request(identity).status, "unknown");
});

test("started file operations cannot claim cancellation", () => {
  const control = new ActionCancellation();
  const request = { ...identity, capability_id: "desktop_fs_write_text" };
  const entry = control.start(request);
  entry.started = true;
  assert.equal(control.request(request).status, "not_cancellable");
  assert.equal(entry.controller.signal.aborted, false);
});

test("independent executions do not share cancellation", () => {
  const control = new ActionCancellation();
  const one = control.start(identity);
  const two = control.start({ ...identity, action_id: "two" });
  control.request(identity);
  assert.equal(one.controller.signal.aborted, true);
  assert.equal(two.controller.signal.aborted, false);
});

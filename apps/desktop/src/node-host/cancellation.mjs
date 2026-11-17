/** Cancellation state belongs to an immutable execution identity, never a handler. */
export class ActionCancellation {
  constructor() { this.active = new Map(); }

  start(request) {
    const entry = { request, controller: new AbortController(), started: false };
    this.active.set(request.action_id, entry);
    return entry;
  }

  request(identity) {
    const entry = this.active.get(identity.action_id);
    if (!entry) return { status: "unknown" };
    if (!["action_id", "request_id", "fencing_token"].every((key) =>
      identity[key] && identity[key] === entry.request[key])) {
      return { status: "identity_mismatch" };
    }
    if (entry.started && entry.request.capability_id !== "desktop_exec") {
      return { status: "not_cancellable" };
    }
    entry.controller.abort();
    return { status: "requested", entry };
  }

  finish(entry) {
    if (this.active.get(entry.request.action_id) === entry) this.active.delete(entry.request.action_id);
  }
}

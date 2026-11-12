export interface Envelope {
  type?: string;
  data?: Record<string, unknown>;
}
/** Thread channel only. Reconnects resnapshot via onReady; events carry no authoritative state. */
export function subscribeThread(
  url: string,
  token: () => string | null,
  onEvent: (event: Envelope) => void,
  onReady: (reason: "connected" | "recovered" | "execution_idle") => void,
): () => void {
  let disposed = false;
  let socket: WebSocket | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0;
  const connect = () => {
    if (disposed) return;
    clearTimeout(timer);
    const previous = socket;
    socket = undefined;
    previous?.close();
    if (!token()) return;
    const current = new WebSocket(url);
    socket = current;
    let ready = false;
    let executionState: unknown;
    current.onopen = () =>
      current.send(
        JSON.stringify({ type: "auth", access_token: token() || "" }),
      );
    current.onmessage = (event) => {
      if (disposed || current !== socket) return;
      let envelope: Envelope;
      try {
        envelope = JSON.parse(String(event.data));
      } catch {
        return;
      }
      // The server sends an authenticated execution_status snapshot on every connection.
      if (envelope.type === "execution_status") {
        const next = envelope.data?.state;
        if (!ready) onReady("connected");
        else if (next === "idle" && (executionState === "running" || executionState === "busy")) onReady("execution_idle");
        ready = true;
        failures = 0;
        executionState = next;
      }
      onEvent(envelope);
    };
    current.onclose = (event) => {
      if (
        disposed ||
        socket !== current ||
        event.code === 1008 ||
        event.code === 4401
      )
        return;
      timer = setTimeout(
        connect,
        Math.min(30000, 1000 * 2 ** Math.min(failures++, 5)),
      );
    };
    current.onerror = () => current.close();
  };
  const recover = () => {
    if (!document.hidden) {
      onReady("recovered");
      connect();
    }
  };
  window.addEventListener("online", recover);
  window.addEventListener("focus", recover);
  window.addEventListener("aiive:resume", recover);
  document.addEventListener("visibilitychange", recover);
  connect();
  return () => {
    disposed = true;
    clearTimeout(timer);
    socket?.close();
    window.removeEventListener("online", recover);
    window.removeEventListener("focus", recover);
    window.removeEventListener("aiive:resume", recover);
    document.removeEventListener("visibilitychange", recover);
  };
}

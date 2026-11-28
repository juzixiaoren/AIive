import { useCallback, useEffect, useRef, useState } from "react";

/** Coordinate the current thread, history and execution-state handshake on both clients. */
export function useConversationBootstrap(enabled: boolean, sync: () => Promise<boolean>) {
  const [phase, setPhase] = useState<"syncing" | "ready" | "failed">("syncing");
  const pending = useRef<Promise<boolean> | null>(null);

  const retry = useCallback(() => {
    if (!enabled) return Promise.resolve(false);
    if (pending.current) return pending.current;
    setPhase("syncing");
    const attempt = sync().then(ok => {
      if (pending.current === attempt) {
        pending.current = null;
        setPhase(ok ? "ready" : "failed");
      }
      return ok;
    });
    pending.current = attempt;
    return attempt;
  }, [enabled, sync]);

  useEffect(() => {
    void retry();
    return () => { pending.current = null; };
  }, [retry]);

  useEffect(() => {
    if (!enabled || phase !== "failed") return;
    const recover = () => { void retry(); };
    window.addEventListener("online", recover);
    window.addEventListener("aiive:resume", recover);
    return () => {
      window.removeEventListener("online", recover);
      window.removeEventListener("aiive:resume", recover);
    };
  }, [enabled, phase, retry]);

  return { ready: enabled && phase === "ready", failed: enabled && phase === "failed", retry };
}

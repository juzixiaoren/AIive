import { useEffect, useState } from "react";
import { apiFetch } from "./api/auth";
import { subscribeThreadEvents } from "./api/chat";

type Reminder = {
  id: string;
  title: string;
  status: "pending" | "alerting";
  event_type: string;
  scheduled_at?: string | null;
};

export default function ReminderInbox() {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const response = await apiFetch("notifications?category=pending");
      if (!response.ok) throw new Error("无法加载提醒。");
      const entries: Reminder[] = await response.json();
      setReminders(entries.filter(item => item.event_type === "reminder_created"));
      setError("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "无法加载提醒。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const update = () => void refresh();
    update();
    window.addEventListener("aiive:resume", update);
    const unsubscribe = subscribeThreadEvents("__global__", () => {}, event => {
      if (event.type === "notification") update();
    });
    return () => {
      unsubscribe();
      window.removeEventListener("aiive:resume", update);
    };
  }, []);

  async function update(reminder: Reminder, action: "confirm" | "snooze" | "cancel") {
    setBusy(reminder.id);
    try {
      const path = `notifications/${encodeURIComponent(reminder.id)}`;
      const response = await apiFetch(action === "cancel" ? path : `${path}/reminder`, action === "cancel"
        ? { method: "DELETE" }
        : {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action, delay_minutes: 5 }),
          });
      if (!response.ok) throw new Error("提醒状态已变化，请刷新后查看。");
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "提醒操作失败。");
    } finally {
      setBusy("");
    }
  }

  return <main className="mobile-work">
    <header className="mobile-work-header">
      <h1>提醒</h1>
      <button disabled={loading} onClick={() => void refresh()}>刷新</button>
    </header>
    {error && <p role="alert">{error}</p>}
    {loading && !reminders.length && <p>正在加载提醒…</p>}
    {!loading && !error && !reminders.length && <p>没有待处理的提醒。</p>}
    {reminders.map(reminder => <article className="mobile-attention" key={reminder.id}>
      <h2>{reminder.title}</h2>
      <p>{reminder.status === "alerting" ? "已到期" : "待提醒"}{reminder.scheduled_at && ` · ${new Date(reminder.scheduled_at).toLocaleString()}`}</p>
      <div className="mobile-actions">
        {reminder.status === "alerting" && <>
          <button disabled={Boolean(busy)} onClick={() => void update(reminder, "confirm")}>确认完成</button>
          <button disabled={Boolean(busy)} onClick={() => void update(reminder, "snooze")}>5 分钟后提醒</button>
        </>}
        <button disabled={Boolean(busy)} onClick={() => void update(reminder, "cancel")}>取消提醒</button>
      </div>
    </article>)}
  </main>;
}

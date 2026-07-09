import { useState, useEffect } from "react";
import ChatPage from "./pages/ChatPage";
import EventTimeline from "./pages/EventTimeline";
import ContextInspector from "./pages/ContextInspector";
import ToolsPage from "./pages/ToolsPage";
import NotificationsPage from "./pages/NotificationsPage";

type Tab = "chat" | "events" | "context" | "tools" | "notifs";

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [inspectTraceId, setInspectTraceId] = useState<string | undefined>();
  const [notifCount, setNotifCount] = useState(0);

  useEffect(() => {
    const poll = () => {
      fetch("/api/notifications").then(r => r.json()).then((d: Array<unknown>) => setNotifCount(d.length)).catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 30000);
    return () => clearInterval(interval);
  }, []);

  const tabs: { key: Tab; label: string }[] = [
    { key: "chat", label: "对话" },
    { key: "events", label: "事件" },
    { key: "context", label: "上下文" },
    { key: "tools", label: "工具" },
    { key: "notifs", label: `通知${notifCount > 0 ? ` ${notifCount}` : ""}` },
  ];

  return (
    <div className="min-h-screen flex flex-col bg-slate-50">
      <header className="border-b border-slate-200 bg-white px-6 py-4 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold tracking-tight text-slate-800">AIive</h1>
          <span className="text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded">个人管家</span>
        </div>
        <div className="flex gap-1 bg-slate-100 rounded-lg p-1">
          {tabs.map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-1.5 text-sm rounded-md transition-all ${
                tab === t.key
                  ? "bg-white text-slate-800 shadow-sm font-medium"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </header>

      <main className="flex-1 w-full max-w-3xl mx-auto py-6 px-4">
        {tab === "chat" && (
          <ChatPage onInspectTrace={(tid) => { setInspectTraceId(tid); setTab("context"); }} />
        )}
        {tab === "events" && <EventTimeline traceId={inspectTraceId} />}
        {tab === "context" && <ContextInspector traceId={inspectTraceId} />}
        {tab === "tools" && <ToolsPage />}
        {tab === "notifs" && <NotificationsPage />}
      </main>
    </div>
  );
}

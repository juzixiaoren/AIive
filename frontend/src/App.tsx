/**
 * 应用根组件
 * - 管理顶部 Tab 导航栏（对话、事件、上下文、工具、通知）
 * - 轮询未读通知数量并在 Tab 标签上显示角标
 * - 支持从对话页面传递 trace_id 到上下文检查器
 */

import { useState, useEffect } from "react";
import ChatPage from "./pages/ChatPage";
import EventTimeline from "./pages/EventTimeline";
import ContextInspector from "./pages/ContextInspector";
import ToolsPage from "./pages/ToolsPage";
import NotificationsPage from "./pages/NotificationsPage";

type Tab = "chat" | "events" | "context" | "tools" | "notifs";

/**
 * 应用根组件
 * 负责顶部导航和各页面模块的切换展示
 */
export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [inspectTraceId, setInspectTraceId] = useState<string | undefined>();
  const [notifCount, setNotifCount] = useState(0);

  // 定时轮询通知数量，用于 Tab 角标显示
  useEffect(() => {
    const poll = () => {
      fetch("/api/notifications?category=pending").then(r => r.json()).then((d: Array<unknown>) => setNotifCount(d.length)).catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 30000);
    return () => clearInterval(interval);
  }, []);

  // 导航标签配置
  const tabs: { key: Tab; label: string }[] = [
    { key: "chat", label: "对话" },
    { key: "events", label: "事件" },
    { key: "context", label: "上下文" },
    { key: "tools", label: "工具" },
    { key: "notifs", label: `通知${notifCount > 0 ? ` ${notifCount}` : ""}` },
  ];

  return (
    <div className="h-screen flex flex-col bg-background overflow-hidden">
      {/* 顶部导航栏 */}
      <header className="border-b border-divider bg-surface px-6 py-4 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold tracking-tight text-content">AIive</h1>
          <span className="text-xs text-faint bg-surface-muted px-2 py-0.5 rounded">个人管家</span>
        </div>
        {/* Tab 切换按钮组 */}
        <div className="flex gap-1 bg-surface-muted rounded-lg p-1">
          {tabs.map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-1.5 text-sm rounded-md transition-all ${
                tab === t.key
                  ? "bg-surface text-content shadow-sm font-medium"
                  : "text-muted hover:text-title"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </header>

      {/* 主内容区域，按当前选中的 Tab 渲染对应页面 */}
      <main className="flex-1 min-h-0 w-full max-w-3xl mx-auto px-4 flex flex-col">
        {tab === "chat" ? (
          // 对话页占满可用高度，输入框贴底
          <ChatPage onInspectTrace={(tid) => { setInspectTraceId(tid); setTab("context"); }} />
        ) : (
          // 其余页面在独立可滚动容器中展示
          <div className="flex-1 min-h-0 overflow-y-auto py-6">
            {tab === "events" && <EventTimeline traceId={inspectTraceId} />}
            {tab === "context" && <ContextInspector traceId={inspectTraceId} />}
            {tab === "tools" && <ToolsPage />}
            {tab === "notifs" && <NotificationsPage />}
          </div>
        )}
      </main>
    </div>
  );
}

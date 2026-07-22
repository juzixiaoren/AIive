/**
 * 应用根组件
 * - 管理顶部 Tab 导航栏（对话、记忆、能力、事件、上下文、检索、通知；开发者页在构建开关开启时显示）
 * - 通过全局通知通道（WebSocket）实时同步未读通知数量并在 Tab 标签上显示角标
 * - 支持从对话页面传递 trace_id 到上下文检查器
 */

import { useEffect, useState } from "react";
import ChatPage from "./pages/ChatPage";
import EventTimeline from "./pages/EventTimeline";
import ContextInspector from "./pages/ContextInspector";
import RetrievalInspector from "./pages/RetrievalInspector";
import MemoriesPage from "./pages/MemoriesPage";
import CapabilitiesPage from "./pages/CapabilitiesPage";
import DeveloperPage from "./pages/DeveloperPage";
import NotificationsPage from "./pages/NotificationsPage";
import { useNotificationCount } from "./hooks/useNotificationSocket";

type Tab = "chat" | "memories" | "capabilities" | "events" | "context" | "retrieval" | "notifs" | "developer";

const developerUiEnabled = (import.meta as ImportMeta & {
  env?: Record<string, string | undefined>;
}).env?.VITE_AIIVE_DEVELOPER_UI_ENABLED === "true";

function tabFromLocation(): Tab {
  return developerUiEnabled && window.location.pathname === "/developer" ? "developer" : "chat";
}

/**
 * 应用根组件
 * 负责顶部导航和各页面模块的切换展示
 */
export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromLocation);
  const [inspectTraceId, setInspectTraceId] = useState<string | undefined>();

  useEffect(() => {
    const handlePopState = () => setTab(tabFromLocation());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = (nextTab: Tab) => {
    const nextPath = nextTab === "developer" ? "/developer" : "/";
    if (window.location.pathname !== nextPath) window.history.pushState({}, "", nextPath);
    setTab(nextTab);
  };
  // 通过全局通知通道实时同步未读角标数量（替代定时轮询）
  const notifCount = useNotificationCount();

  // 导航标签配置
  const tabs: { key: Tab; label: string }[] = [
    { key: "chat", label: "对话" },
    { key: "memories", label: "记忆" },
    { key: "capabilities", label: "能力" },
    { key: "events", label: "事件" },
    { key: "context", label: "上下文" },
    { key: "retrieval", label: "检索" },
    { key: "notifs", label: `通知${notifCount > 0 ? ` ${notifCount}` : ""}` },
    ...(developerUiEnabled ? [{ key: "developer" as Tab, label: "开发者" }] : []),
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
              onClick={() => navigate(t.key)}
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
          <ChatPage onInspectTrace={(tid) => { setInspectTraceId(tid); navigate("context"); }} />
        ) : (
          // 其余页面在独立可滚动容器中展示
          <div className="flex-1 min-h-0 overflow-y-auto py-6">
            {tab === "memories" && <MemoriesPage />}
            {tab === "capabilities" && <CapabilitiesPage />}
            {tab === "events" && <EventTimeline traceId={inspectTraceId} />}
            {tab === "context" && <ContextInspector traceId={inspectTraceId} />}
            {tab === "retrieval" && <RetrievalInspector traceId={inspectTraceId} />}
            {tab === "notifs" && <NotificationsPage />}
            {tab === "developer" && <DeveloperPage selectedTraceId={inspectTraceId} />}
          </div>
        )}
      </main>
    </div>
  );
}

/**
 * 应用根组件
 * - 管理顶部 Tab 导航栏（对话、记忆、能力、事件、上下文、检索、通知；开发者页在构建开关开启时显示）
 * - 通过全局通知通道（WebSocket）实时同步未读通知数量并在 Tab 标签上显示角标
 * - 支持从对话页面传递 trace_id 到上下文检查器
 */

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { BASE_PATH } from "./lib/base";
import { useNotificationCount } from "./hooks/useNotificationSocket";

const ChatPage = lazy(() => import("./pages/ChatPage"));
const EventTimeline = lazy(() => import("./pages/EventTimeline"));
const ContextInspector = lazy(() => import("./pages/ContextInspector"));
const RetrievalInspector = lazy(() => import("./pages/RetrievalInspector"));
const MemoriesPage = lazy(() => import("./pages/MemoriesPage"));
const CapabilitiesPage = lazy(() => import("./pages/CapabilitiesPage"));
const DeveloperPage = lazy(() => import("./pages/DeveloperPage"));
const NotificationsPage = lazy(() => import("./pages/NotificationsPage"));
const TaskCenterPage = lazy(() => import("./pages/TaskCenterPage"));

type Tab = "chat" | "tasks" | "memories" | "capabilities" | "events" | "context" | "retrieval" | "notifs" | "developer";

const developerUiEnabled = (import.meta as ImportMeta & {
  env?: Record<string, string | undefined>;
}).env?.VITE_AIIVE_DEVELOPER_UI_ENABLED === "true";

function tabFromLocation(): Tab {
  return developerUiEnabled && window.location.pathname.startsWith(`${BASE_PATH}developer`) ? "developer" : "chat";
}

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
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
    const nextPath = nextTab === "developer" ? `${BASE_PATH}developer` : BASE_PATH;
    if (window.location.pathname !== nextPath) window.history.pushState({}, "", nextPath);
    setTab(nextTab);
  };
  // 通过全局通知通道实时同步未读角标数量（替代定时轮询）
  const notifCount = useNotificationCount();

  // 导航标签配置
  const tabs: { key: Tab; label: string }[] = [
    { key: "chat", label: "对话" },
    { key: "tasks", label: "任务" },
    { key: "memories", label: "记忆" },
    { key: "capabilities", label: "能力" },
    { key: "events", label: "事件" },
    { key: "context", label: "上下文" },
    { key: "retrieval", label: "检索" },
    { key: "notifs", label: `通知${notifCount > 0 ? ` ${notifCount}` : ""}` },
    ...(developerUiEnabled ? [{ key: "developer" as Tab, label: "开发者" }] : []),
  ];

  // 双主题：null = 跟随系统；"light"/"dark" = 手动覆盖
  const getSystemDark = useCallback(
    () => window.matchMedia("(prefers-color-scheme: dark)").matches,
    []
  );
  const [mode, setMode] = useState<"light" | "dark" | null>(null);
  const [systemDark, setSystemDark] = useState(getSystemDark);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => setSystemDark(mq.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  const effectiveMode: "light" | "dark" = mode ?? (systemDark ? "dark" : "light");
  const toggleMode = useCallback(() => {
    setMode((prev) => {
      const current = prev ?? (getSystemDark() ? "dark" : "light");
      return current === "dark" ? "light" : "dark";
    });
  }, [getSystemDark]);

  return (
    <div
      className={
        "site-shell h-screen flex flex-col bg-background overflow-hidden" +
        (mode ? ` mode-${mode}` : "")
      }
    >
      {/* 顶部导航栏 */}
      <header className="border-b border-divider bg-surface px-6 py-4 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-3">
          <img
            src={`${BASE_PATH}aiive-logo.png`}
            alt="AIive Logo"
            className="h-10 w-10 object-contain drop-shadow-sm"
          />
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

        {/* 主题切换（复用个人站双主题机制，可跟随系统） */}
        <button
          type="button"
          onClick={toggleMode}
          className="flex items-center gap-1.5 rounded-md border border-divider px-3 py-1.5 text-sm font-medium text-content transition-colors hover:bg-surface-muted"
          title={effectiveMode === "dark" ? "切换到浅色" : "切换到深色"}
          aria-label="切换主题"
        >
          {effectiveMode === "dark" ? <SunIcon /> : <MoonIcon />}
          <span>{effectiveMode === "dark" ? "浅色" : "深色"}</span>
        </button>
      </header>

      {/* 主内容区域，按当前选中的 Tab 渲染对应页面 */}
      <main className={`flex-1 min-h-0 w-full mx-auto px-4 flex flex-col ${tab === "tasks" ? "max-w-6xl" : "max-w-3xl"}`}>
        <Suspense fallback={<div className="py-8 text-sm text-muted">加载中…</div>}>
          {tab === "chat" ? (
            // 对话页占满可用高度，输入框贴底
            <ChatPage onInspectTrace={(tid) => { setInspectTraceId(tid); navigate("context"); }} />
          ) : (
            // 其余页面在独立可滚动容器中展示
            <div className="flex-1 min-h-0 overflow-y-auto py-6">
              {tab === "memories" && <MemoriesPage />}
              {tab === "tasks" && <TaskCenterPage />}
              {tab === "capabilities" && <CapabilitiesPage />}
              {tab === "events" && <EventTimeline traceId={inspectTraceId} />}
              {tab === "context" && <ContextInspector traceId={inspectTraceId} />}
              {tab === "retrieval" && <RetrievalInspector traceId={inspectTraceId} />}
              {tab === "notifs" && <NotificationsPage />}
              {tab === "developer" && <DeveloperPage selectedTraceId={inspectTraceId} />}
            </div>
          )}
        </Suspense>
      </main>
    </div>
  );
}

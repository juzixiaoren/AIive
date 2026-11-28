/**
 * 应用根组件
 * - 认证门禁（V49）：未登录只渲染登录页，登录后才挂载私人数据页面与通知通道
 * - 管理顶部 Tab 导航栏（对话、记忆、能力、事件、上下文、检索、通知；开发者页在构建开关开启时显示）
 * - 通过全局通知通道（WebSocket）实时同步未读通知数量并在 Tab 标签上显示角标
 * - 支持从对话页面传递 trace_id 到上下文检查器
 */

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { BASE_PATH } from "./lib/base";
import { useNotificationCount } from "./hooks/useNotificationSocket";
import { AuthProvider, useAuth } from "./hooks/useAuth";
import { apiFetch } from "./api/client";
import { ToolTrustControl } from "../../shared/settings/ToolTrustControl";

const fetchSettings = (path: string, init?: RequestInit) => apiFetch(`${BASE_PATH}api/${path}`, init);

const ChatPage = lazy(() => import("./pages/ChatPage"));
const EventTimeline = lazy(() => import("./pages/EventTimeline"));
const ContextInspector = lazy(() => import("./pages/ContextInspector"));
const RetrievalInspector = lazy(() => import("./pages/RetrievalInspector"));
const MemoriesPage = lazy(() => import("./pages/MemoriesPage"));
const CapabilitiesPage = lazy(() => import("./pages/CapabilitiesPage"));
const DeveloperPage = lazy(() => import("./pages/DeveloperPage"));
const NotificationsPage = lazy(() => import("./pages/NotificationsPage"));
const TaskCenterPage = lazy(() => import("./pages/TaskCenterPage"));
const LoginPage = lazy(() => import("./pages/LoginPage"));
const SelfEvolutionPage = lazy(() => import("./pages/SelfEvolutionPage"));

type Tab = "chat" | "tasks" | "memories" | "capabilities" | "events" | "context" | "retrieval" | "notifs" | "developer" | "evolution";

const tabPaths: Record<Tab, string> = {
  chat: "", tasks: "work", memories: "memories", capabilities: "capabilities",
  events: "events", context: "context", retrieval: "retrieval", notifs: "notifications",
  developer: "developer", evolution: "evolution",
};
const traceTabs: Tab[] = ["context", "events", "retrieval", "developer"];

const developerUiEnabled = (import.meta as ImportMeta & {
  env?: Record<string, string | undefined>;
}).env?.VITE_AIIVE_DEVELOPER_UI_ENABLED === "true";

const loginPath = `${BASE_PATH}login`;

function loginDestination(): string {
  return safeInternalPath(window.history.state?.returnTo) ?? BASE_PATH;
}

function routeFromLocation() {
  const url = window.location.pathname === loginPath
    ? new URL(loginDestination(), window.location.href)
    : new URL(window.location.href);
  const tab = (Object.keys(tabPaths) as Tab[]).find(key =>
    (key !== "developer" || developerUiEnabled) && url.pathname === BASE_PATH + tabPaths[key]
  ) ?? "chat";
  return {
    tab,
    taskId: tab === "tasks" ? url.searchParams.get("task") ?? "" : "",
    traceId: traceTabs.includes(tab) ? url.searchParams.get("trace") ?? undefined : undefined,
  };
}

function tabHref(tab: Tab, selection?: string) {
  const path = BASE_PATH + tabPaths[tab];
  if (!selection) return path;
  return path + "?" + new URLSearchParams({ [tab === "tasks" ? "task" : "trace"]: selection });
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

/** 只接受站内路径，拒绝协议相对地址与外部 URL，避免开放重定向。 */
function safeInternalPath(candidate: unknown): string | null {
  if (typeof candidate !== "string" || !candidate.startsWith(BASE_PATH) ||
      candidate.startsWith("//") || candidate.includes("\\")) return null;
  try {
    const url = new URL(candidate, window.location.href);
    if (url.origin === window.location.origin && url.pathname.startsWith(BASE_PATH) &&
        !url.pathname.startsWith("//") && url.pathname !== loginPath) {
      return url.pathname + url.search + url.hash;
    }
  } catch {
    // Malformed or stale history entries fall back to the app root.
  }
  return null;
}

/**
 * 认证门禁：loading 只显示占位，未登录只显示登录页。
 * 登录成功后回到原本请求的站内页面，没有有效目标时进入对话页。
 */
function AppGate() {
  const { phase, username } = useAuth();
  useEffect(() => {
    const { pathname, search, hash } = window.location;
    if (phase === "anonymous" && pathname !== loginPath) {
      window.history.replaceState({ returnTo: pathname + search + hash }, "", loginPath);
    } else if (phase === "authenticated" && pathname === loginPath) {
      window.history.replaceState({}, "", loginDestination());
    }
  }, [phase]);

  if (phase === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-sm text-muted">
        正在验证登录状态…
      </div>
    );
  }
  if (phase === "anonymous") {
    return (
      <Suspense fallback={<div className="py-8 text-center text-sm text-muted">加载中…</div>}>
        <LoginPage />
      </Suspense>
    );
  }
  return <AuthenticatedApp username={username} />;
}

/**
 * 已登录应用主体
 * 负责顶部导航和各页面模块的切换展示
 */
function AuthenticatedApp({ username }: { username: string }) {
  const { logout } = useAuth();
  const [{ tab, taskId, traceId }, setRoute] = useState(routeFromLocation);

  useEffect(() => {
    const handlePopState = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = (href: string) => {
    const { pathname, search, hash } = window.location;
    if (pathname + search + hash === href) return;
    window.history.pushState({}, "", href);
    setRoute(routeFromLocation());
  };
  const openWork = (id: string) => navigate(tabHref("tasks", id));
  // 通过全局通知通道实时同步未读角标数量（替代定时轮询）
  const notifCount = useNotificationCount();

  // 导航标签配置
  const tabs: { key: Tab; label: string }[] = [
    { key: "chat", label: "对话" },
    { key: "tasks", label: "工作" },
    { key: "memories", label: "记忆" },
    { key: "capabilities", label: "能力" },
    { key: "evolution", label: "自进化" },
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
      <header className="border-b border-divider bg-surface px-3 sm:px-6 py-3 sm:py-4 flex flex-wrap sm:flex-nowrap items-center justify-between gap-3 shadow-sm">
        <div className="flex items-center gap-2 sm:gap-3 shrink-0">
          <img
            src={`${BASE_PATH}aiive-logo.png`}
            alt="AIive Logo"
            className="h-8 w-8 sm:h-10 sm:w-10 object-contain drop-shadow-sm"
          />
          <h1 className="text-xl font-bold tracking-tight text-content">AIive</h1>
          <span className="hidden sm:inline text-xs text-faint bg-surface-muted px-2 py-0.5 rounded">个人管家</span>
        </div>
        {/* Tab 切换按钮组 */}
        <nav aria-label="主导航" className="order-last w-full sm:order-none sm:w-auto flex gap-1 bg-surface-muted rounded-lg p-1 overflow-x-auto">
          {tabs.map(t => {
            const href = t.key === tab
              ? tabHref(t.key, taskId || traceId)
              : tabHref(t.key, traceTabs.includes(t.key) ? traceId : undefined);
            return <a
              key={t.key}
              href={href}
              aria-current={tab === t.key ? "page" : undefined}
              onClick={event => {
                if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                if (tab !== t.key) navigate(href);
              }}
              className={`px-4 py-1.5 text-sm rounded-md transition-all whitespace-nowrap ${
                tab === t.key
                  ? "bg-surface text-content shadow-sm font-medium"
                  : "text-muted hover:text-title"
              }`}
            >
              {t.label}
            </a>;
          })}
        </nav>

        <div className="flex items-center gap-2 shrink-0">
          <ToolTrustControl apiFetch={fetchSettings} />
          {/* 账号标识与退出登录 */}
          <span className="hidden md:inline max-w-[10rem] truncate text-xs text-muted" title={username}>
            {username}
          </span>
          <button
            type="button"
            onClick={logout}
            className="rounded-md border border-divider px-3 py-1.5 text-sm font-medium text-content transition-colors hover:bg-surface-muted"
            title="退出登录"
          >
            退出登录
          </button>
          {/* 主题切换（复用个人站双主题机制，可跟随系统） */}
          <button
            type="button"
            onClick={toggleMode}
            className="flex items-center gap-1.5 rounded-md border border-divider px-3 py-1.5 text-sm font-medium text-content transition-colors hover:bg-surface-muted"
            title={effectiveMode === "dark" ? "切换到浅色" : "切换到深色"}
            aria-label="切换主题"
          >
            {effectiveMode === "dark" ? <SunIcon /> : <MoonIcon />}
            <span className="hidden sm:inline">{effectiveMode === "dark" ? "浅色" : "深色"}</span>
          </button>
        </div>
      </header>

      {/* 主内容区域，按当前选中的 Tab 渲染对应页面 */}
      <main className={`flex-1 min-h-0 w-full mx-auto px-4 flex flex-col ${tab === "tasks" ? "max-w-6xl" : "max-w-3xl"}`}>
        <Suspense fallback={<div className="py-8 text-sm text-muted">加载中…</div>}>
          <div className={tab === "chat" ? "flex flex-col flex-1 min-h-0" : "hidden"}>
            {/* 对话持续挂载，切换工作中心不结束 SSE。 */}
            <ChatPage active={tab === "chat"} onOpenWork={openWork} onInspectTrace={id => navigate(tabHref("context", id))} />
          </div>
        </Suspense>
        <Suspense fallback={<div className="py-8 text-sm text-muted">加载中…</div>}>
          {tab !== "chat" && (
            <div className="flex-1 min-h-0 overflow-y-auto py-6">
              {tab === "memories" && <MemoriesPage />}
              {tab === "tasks" && <TaskCenterPage taskId={taskId} onSelectTask={openWork} />}
              {tab === "capabilities" && <CapabilitiesPage />}
              {tab === "evolution" && <SelfEvolutionPage onOpenWork={openWork} />}
              {tab === "events" && <EventTimeline traceId={traceId} />}
              {tab === "context" && <ContextInspector traceId={traceId} />}
              {tab === "retrieval" && <RetrievalInspector traceId={traceId} />}
              {tab === "notifs" && <NotificationsPage />}
              {tab === "developer" && <DeveloperPage selectedTraceId={traceId} />}
            </div>
          )}
        </Suspense>
      </main>
    </div>
  );
}

/** 应用入口：先建立全局认证状态，再渲染门禁。 */
export default function App() {
  return (
    <AuthProvider>
      <AppGate />
    </AuthProvider>
  );
}

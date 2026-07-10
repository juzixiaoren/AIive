/**
 * 通知页面
 * - 展示系统通知列表（提醒类通知为主）
 * - 支持删除通知（从数据库完整移除）
 * - 通知状态包括：待提醒、提醒中、已确认、已延时
 */

import { useEffect, useState } from "react";

/** 通知数据结构 */
interface Notif {
  id: string;
  title: string;
  message: string;
  event_type: string;
  status: string;
  thread_id: string;
  created_at: string;
}

/** 通知状态中文标签映射 */
const STATUS_LABELS: Record<string, string> = {
  pending: "待提醒",
  alerting: "提醒中",
  confirmed: "已确认",
  snoozed: "已延时",
  cancelled: "已取消",
};
/** 通知状态对应颜色样式 */
const STATUS_COLORS: Record<string, string> = {
  pending: "bg-warning-soft text-warning-text",
  alerting: "bg-danger-soft text-danger-text",
  confirmed: "bg-success-soft text-success-text",
  snoozed: "bg-surface-muted text-muted",
  cancelled: "bg-surface-muted text-faint",
};

/**
 * 通知页面组件
 */
export default function NotificationsPage() {
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [category, setCategory] = useState<"pending" | "done">("pending");

  /** 刷新通知列表 */
  const refresh = (cat?: "pending" | "done") => {
    const c = cat ?? category;
    fetch(`/api/notifications?category=${c}`)
      .then((r) => r.json())
      .then(setNotifs)
      .catch(() => setNotifs([]));
  };

  // 组件挂载时加载通知列表
  useEffect(() => {
    refresh();
  }, []);

  /** 切换分类 */
  const switchCategory = (cat: "pending" | "done") => {
    setCategory(cat);
    refresh(cat);
  };

  /** 删除通知：调后端 DELETE 接口从数据库完整移除 */
  const handleDelete = async (n: Notif) => {
    setBusyId(n.id);
    try {
      const res = await fetch(`/api/notifications/${n.id}`, { method: "DELETE" });
      const data = await res.json();
      if (data.ok) {
        setNotifs((prev) => prev.filter((item) => item.id !== n.id));
      }
    } catch {
      // 静默失败，保留通知不变动
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold text-content">通知</h2>
        {/* 分类切换 */}
        <div className="flex gap-1 bg-surface-muted rounded-lg p-0.5">
          <button
            onClick={() => switchCategory("pending")}
            className={`px-3 py-1 text-xs rounded-md transition-all ${
              category === "pending"
                ? "bg-surface text-content shadow-sm font-medium"
                : "text-muted hover:text-title"
            }`}
          >
            未执行
          </button>
          <button
            onClick={() => switchCategory("done")}
            className={`px-3 py-1 text-xs rounded-md transition-all ${
              category === "done"
                ? "bg-surface text-content shadow-sm font-medium"
                : "text-muted hover:text-title"
            }`}
          >
            已执行
          </button>
        </div>
      </div>
      {notifs.length === 0 && (
        <div className="text-center text-faint text-sm py-12">暂无通知</div>
      )}
      <div className="flex flex-col gap-2">
        {notifs.map((n) => (
          <div key={n.id} className="bg-surface border border-divider rounded-lg p-3 shadow-sm">
            {/* 通知标题和状态标签 */}
            <div className="flex items-center gap-2">
              <div className="text-sm font-medium text-content">{n.title}</div>
              {n.status && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[n.status] || "bg-surface-muted text-muted"}`}>
                  {STATUS_LABELS[n.status] || n.status}
                </span>
              )}
              {n.event_type === "reminder_created" && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary-soft text-primary-hover font-medium">提醒</span>
              )}
            </div>
            {/* 通知内容 */}
            <div className="text-xs text-muted mt-1">{n.message}</div>
            {/* 创建时间和删除按钮 */}
            <div className="flex items-center justify-between mt-2">
              <div className="text-[10px] text-faint font-mono">{n.created_at?.slice(11, 19)}</div>
              <button
                disabled={busyId === n.id}
                onClick={() => handleDelete(n)}
                className="text-xs px-2.5 py-1 rounded bg-danger text-on-primary disabled:opacity-50 hover:bg-danger-hover transition-colors"
              >
                {busyId === n.id ? "…" : "删除"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

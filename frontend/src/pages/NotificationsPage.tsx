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
  pending: "bg-amber-100 text-amber-700",
  alerting: "bg-red-100 text-red-700",
  confirmed: "bg-green-100 text-green-700",
  snoozed: "bg-slate-100 text-slate-500",
  cancelled: "bg-gray-100 text-gray-400",
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
        <h2 className="text-lg font-semibold text-slate-800">通知</h2>
        {/* 分类切换 */}
        <div className="flex gap-1 bg-slate-100 rounded-lg p-0.5">
          <button
            onClick={() => switchCategory("pending")}
            className={`px-3 py-1 text-xs rounded-md transition-all ${
              category === "pending"
                ? "bg-white text-slate-800 shadow-sm font-medium"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            未执行
          </button>
          <button
            onClick={() => switchCategory("done")}
            className={`px-3 py-1 text-xs rounded-md transition-all ${
              category === "done"
                ? "bg-white text-slate-800 shadow-sm font-medium"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            已执行
          </button>
        </div>
      </div>
      {notifs.length === 0 && (
        <div className="text-center text-slate-400 text-sm py-12">暂无通知</div>
      )}
      <div className="flex flex-col gap-2">
        {notifs.map((n) => (
          <div key={n.id} className="bg-white border border-slate-200 rounded-lg p-3 shadow-sm">
            {/* 通知标题和状态标签 */}
            <div className="flex items-center gap-2">
              <div className="text-sm font-medium text-slate-800">{n.title}</div>
              {n.status && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[n.status] || "bg-slate-100 text-slate-500"}`}>
                  {STATUS_LABELS[n.status] || n.status}
                </span>
              )}
              {n.event_type === "reminder_created" && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 font-medium">提醒</span>
              )}
            </div>
            {/* 通知内容 */}
            <div className="text-xs text-slate-500 mt-1">{n.message}</div>
            {/* 创建时间和删除按钮 */}
            <div className="flex items-center justify-between mt-2">
              <div className="text-[10px] text-slate-400 font-mono">{n.created_at?.slice(11, 19)}</div>
              <button
                disabled={busyId === n.id}
                onClick={() => handleDelete(n)}
                className="text-xs px-2.5 py-1 rounded bg-red-500 text-white disabled:opacity-50 hover:bg-red-600 transition-colors"
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

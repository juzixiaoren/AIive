import { useEffect, useState } from "react";

interface Notif {
  id: string; title: string; message: string; created_at: string;
}

export default function NotificationsPage() {
  const [notifs, setNotifs] = useState<Notif[]>([]);

  useEffect(() => {
    fetch("/api/notifications").then(r => r.json()).then(setNotifs);
  }, []);

  return (
    <div>
      <h2 className="text-lg font-semibold text-slate-800 mb-4">通知</h2>
      {notifs.length === 0 && <div className="text-center text-slate-400 text-sm py-12">暂无通知</div>}
      <div className="flex flex-col gap-2">
        {notifs.map(n => (
          <div key={n.id} className="bg-white border border-slate-200 rounded-lg p-3 shadow-sm">
            <div className="text-sm font-medium text-slate-800">{n.title}</div>
            <div className="text-xs text-slate-500 mt-1">{n.message}</div>
            <div className="text-[10px] text-slate-400 mt-1 font-mono">{n.created_at?.slice(11, 19)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

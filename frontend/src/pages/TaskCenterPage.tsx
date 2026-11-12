import { useState } from "react";
import { buckets, statusLabel, type Bucket } from "../../../shared/work/types";
import { useWorkCenter } from "../hooks/useWorkCenter";
import WorkDetail from "../components/WorkDetail";
import "./WorkCenter.css";

export default function TaskCenterPage({ taskId = "" }: { taskId?: string }) {
  const [selected, setSelected] = useState(taskId);
  const { controller, state, connectionError } = useWorkCenter(selected);
  return (
    <div className="work-center">
      <div className="work-heading">
        <div>
          <h1>工作</h1>
          <p>从对话委派，在这里查看进展、处理事项和获取成果。</p>
        </div>
        <button
          onClick={() => void controller.refresh()}
          disabled={state.loading}
        >
          刷新
        </button>
      </div>
      {connectionError && <p role="status">{connectionError}</p>}
      {state.error && <p role="alert">{state.error}</p>}
      <div className="work-buckets" role="tablist" aria-label="工作分类">
        {Object.entries(buckets).map(([bucket, label]) => (
          <button
            key={bucket}
            role="tab"
            aria-selected={state.bucket === bucket}
            onClick={() => controller.bucket(bucket as Bucket)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="work-layout">
        <aside className="work-list" aria-label="工作列表">
          {!state.items.length && (
            <p>{state.loading ? "正在读取工作…" : "这个分类暂时没有工作。"}</p>
          )}
          {state.items.map((item) => (
            <button
              key={item.task_id}
              aria-current={item.task_id === selected}
              onClick={() => setSelected(item.task_id)}
            >
              <strong>{item.title}</strong>
              <span>{statusLabel(item.task_status)}</span>
              {item.primary_attention && (
                <small>
                  {item.primary_attention.title} · {item.attention_count}{" "}
                  项待处理
                </small>
              )}
              {item.progress && (
                <small>
                  {item.progress.done} / {item.progress.total} 步完成
                </small>
              )}
            </button>
          ))}
          {state.more && (
            <button
              disabled={state.loading}
              onClick={() => void controller.more()}
            >
              加载更多工作
            </button>
          )}
        </aside>
        <WorkDetail key={selected} state={state} controller={controller} />
      </div>
    </div>
  );
}

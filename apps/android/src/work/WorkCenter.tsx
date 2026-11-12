import { useState } from "react";
import {
  buckets,
  deviceLabel,
  statusLabel,
  taskTypeLabel,
  type Attention,
  type Bucket,
} from "../../../../shared/work/types";
import {
  actionLabels,
  attentionHelp,
  goalControls,
  performAttention,
  type PlanChanges,
} from "../../../../shared/work/operations";
import type {
  WorkController,
  WorkState,
} from "../../../../shared/work/controller";
import { useWorkCenter } from "./useWorkCenter";
import WorkFile from "./WorkFile";
import PlanEditor from "./PlanEditor";
import "./work.css";

function AttentionCard({
  item,
  state,
  controller,
  onEdit,
}: {
  item: Attention;
  state: WorkState;
  controller: WorkController;
  onEdit: (item: Attention, answer: string) => void;
}) {
  const [answer, setAnswer] = useState("");
  return (
    <article className="mobile-attention">
      <h3>{item.title}</h3>
      <p>{item.description}</p>
      <p className="mobile-hint">{attentionHelp[item.kind]}</p>
      {item.expires_at && (
        <small>有效期至 {new Date(item.expires_at).toLocaleString()}</small>
      )}
      {item.preview && <pre>{JSON.stringify(item.preview, null, 2)}</pre>}
      {["goal_question", "task_question"].includes(item.kind) && (
        <label>
          你的回答
          <textarea
            maxLength={2000}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
          />
        </label>
      )}
      <div className="mobile-actions">
        {item.allowed_actions.map((action) => (
          <button
            key={action}
            disabled={
              state.busy ||
              (["answer", "send_input"].includes(action) && !answer.trim())
            }
            onClick={() => {
              if (action === "modify" || action === "modify_plan") {
                onEdit(item, answer);
                return;
              }
              if (
                action === "deny" &&
                !window.confirm("拒绝操作会使当前工作失败，确认拒绝？")
              )
                return;
              if (
                action === "reject" &&
                !window.confirm(
                  item.kind === "goal_question"
                    ? "跳过当前步骤？目标不会被拒绝。"
                    : "拒绝整个目标？",
                )
              )
                return;
              void controller
                .act(() =>
                  performAttention(
                    controller.api,
                    state.selected,
                    item,
                    action,
                    answer,
                  ),
                )
                .then((ok) => {
                  if (ok) setAnswer("");
                });
            }}
          >
            {item.kind === "goal_question" && action === "reject"
              ? "跳过当前步骤"
              : actionLabels[action] || action}
          </button>
        ))}
      </div>
    </article>
  );
}

function WorkDetail({
  state,
  controller,
}: {
  state: WorkState;
  controller: WorkController;
}) {
  const [editing, setEditing] = useState<{
    item?: Attention;
    answer: string;
  } | null>(null);
  const view = state.view;
  if (!view)
    return (
      <p>{state.loading ? "正在读取详情…" : "工作不可用，请刷新后重试。"}</p>
    );
  const goal = view.goal,
    task = view.task;
  const save = (changes: PlanChanges) => {
    void controller
      .act(() =>
        editing?.item
          ? performAttention(
              controller.api,
              task.task_id,
              editing.item,
              editing.item.kind === "goal_question" ? "modify_plan" : "modify",
              editing.answer || "按修改后的计划继续",
              changes,
            )
          : controller.api.goal(task.task_id, "modify", changes),
      )
      .then((ok) => {
        if (ok) setEditing(null);
      });
  };
  return (
    <div className="mobile-detail">
      <section>
        <small>
          {taskTypeLabel(task.task_type)} · {statusLabel(task.task_status)}
        </small>
        <h1>{task.title}</h1>
        <p>{task.goal}</p>
        <p className="mobile-hint">{deviceLabel(view.target_device)}</p>
        <small>更新于 {new Date(task.updated_at).toLocaleString()}</small>
        {task.last_error && <p role="alert">{task.last_error}</p>}
        {view.data_warning && <p>部分记录未完整读取，请刷新检查。</p>}
      </section>
      {view.attention_items.length > 0 && (
        <section>
          <h2>待你处理</h2>
          {view.attention_items.map((item) => (
            <AttentionCard
              key={item.id}
              item={item}
              state={state}
              controller={controller}
              onEdit={(item, answer) => setEditing({ item, answer })}
            />
          ))}
        </section>
      )}
      {goal && (
        <section>
          <h2>目标计划 · {statusLabel(goal.status)}</h2>
          <p>{goal.summary}</p>
          <progress
            aria-label="目标步骤完成进度"
            value={goal.progress.done}
            max={Math.max(1, goal.progress.total)}
          />
          <p>
            {goal.progress.done} / {goal.progress.total} 步完成 ·{" "}
            {goal.progress.waiting} 步等待 · {goal.progress.failed} 步失败
          </p>
          {goal.deadline && (
            <small>
              截止 {new Date(goal.deadline).toLocaleString()} · {goal.timezone}
            </small>
          )}
          {goal.blocked_reason && <p>{goal.blocked_reason}</p>}
          <ul>
            {goal.acceptance.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
          <ol className="mobile-steps">
            {goal.steps.map((step) => (
              <li key={step.id}>
                <h3>{step.title}</h3>
                <small>
                  {statusLabel(step.status)} ·{" "}
                  {
                    (
                      {
                        autonomous: "自动执行",
                        interactive: "用户参与",
                        approval_required: "步骤审批",
                      } as Record<string, string>
                    )[step.kind]
                  }
                </small>
                {step.note && <p>{step.note}</p>}
                {step.next_trigger_at && (
                  <p>
                    下次触发 {new Date(step.next_trigger_at).toLocaleString()}
                  </p>
                )}
                <ul>
                  {step.acceptance.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
          <div className="mobile-actions">
            {goalControls(goal.status).map((action) => (
              <button
                disabled={state.busy}
                key={action}
                onClick={() => {
                  if (action === "modify") {
                    setEditing({ answer: "" });
                    return;
                  }
                  if (
                    ["end", "reject"].includes(action) &&
                    !window.confirm(
                      action === "end"
                        ? "结束目标？已启动的外部操作可能仍会完成。"
                        : "拒绝整个目标？",
                    )
                  )
                    return;
                  void controller.act(() =>
                    controller.api.goal(task.task_id, action),
                  );
                }}
              >
                {
                  {
                    confirm: "确认计划",
                    modify: "修改计划",
                    reject: "拒绝目标",
                    pause: "暂停",
                    resume: "恢复",
                    end: "结束目标",
                  }[action]
                }
              </button>
            ))}
          </div>
          {view.bucket !== "ended" && (
            <p className="mobile-hint">
              暂停不保证立即中断当前操作；结束不会撤销已启动的外部副作用。
            </p>
          )}
          {editing && (
            <PlanEditor
              key={`${task.task_id}:${goal.revision}`}
              goal={goal}
              busy={state.busy}
              onSave={save}
              onClose={() => setEditing(null)}
            />
          )}
        </section>
      )}
      {!goal && view.bucket !== "ended" && (
        <section>
          <button
            disabled={state.busy}
            onClick={() => {
              if (window.confirm("取消工作？已启动的外部操作可能仍会完成。"))
                void controller.act(() => controller.api.cancel(task.task_id));
            }}
          >
            取消工作
          </button>
          <p className="mobile-hint">取消不保证已启动的外部副作用立即停止。</p>
        </section>
      )}
      <section>
        <h2>执行时间线</h2>
        {!state.timeline.length && <p>暂无可见事件。</p>}
        <ol className="mobile-timeline">
          {state.timeline.map((event) => (
            <li key={event.sequence}>
              <strong>{event.title}</strong>
              <small>{new Date(event.created_at).toLocaleString()}</small>
              <p>{event.summary}</p>
            </li>
          ))}
        </ol>
        {state.historyMore && (
          <button
            disabled={state.loading}
            onClick={() => void controller.history()}
          >
            读取更早的记录
          </button>
        )}
      </section>
      <section>
        <h2>成果与证据</h2>
        {task.report && (
          <article className="mobile-file">
            <h3>工作报告</h3>
            <p>{task.report.summary}</p>
          </article>
        )}
        {!task.report && !view.artifacts.length && !view.evidence.length && (
          <p>暂时没有成果。</p>
        )}
        {view.artifacts.map((file) => (
          <WorkFile
            key={file.id}
            task={task.task_id}
            file={file}
            kind="artifacts"
          />
        ))}
        {view.evidence.map((file) => (
          <WorkFile
            key={file.id}
            task={task.task_id}
            file={file}
            kind="evidence"
          />
        ))}
        {(view.truncated.task_artifact || view.truncated.task_evidence) && (
          <p>仅展示最近的 50 项可见成果或证据。</p>
        )}
      </section>
      <details>
        <summary>技术执行详情（{view.actions.length}）</summary>
        <p>结果未知的操作等待设备 Journal 对账，不提供普通重试。</p>
        {view.actions.map((action) => (
          <article key={action.id}>
            <h3>{action.capability_id}</h3>
            <p>
              {statusLabel(action.status)} · 风险 {action.risk_level}
            </p>
            <small>{action.id}</small>
          </article>
        ))}
      </details>
    </div>
  );
}

export default function WorkCenter({
  taskId = "",
  onBack,
}: {
  taskId?: string;
  onBack?: () => void;
}) {
  const [selected, setSelected] = useState(taskId);
  const { controller, state, connectionError } = useWorkCenter(selected);
  return (
    <main className="mobile-work">
      <header className="mobile-work-header">
        {selected ? (
          <button
            onClick={() => {
              setSelected("");
              onBack?.();
            }}
          >
            ‹ 工作列表
          </button>
        ) : (
          <h1>工作</h1>
        )}
        <button
          disabled={state.loading}
          onClick={() => void controller.refresh()}
        >
          刷新
        </button>
      </header>
      {state.error && <p role="alert">{state.error}</p>}
      {connectionError && <p role="status">{connectionError}</p>}
      {selected ? (
        <WorkDetail key={selected} state={state} controller={controller} />
      ) : (
        <>
          <p className="mobile-hint">从对话委派，在这里接着处理。</p>
          <div className="mobile-buckets" role="tablist" aria-label="工作分类">
            {Object.entries(buckets).map(([key, label]) => (
              <button
                role="tab"
                aria-selected={key === state.bucket}
                key={key}
                onClick={() => controller.bucket(key as Bucket)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="mobile-inbox">
            {!state.items.length && (
              <p>
                {state.loading ? "正在读取工作…" : "这个分类暂时没有工作。"}
              </p>
            )}
            {state.items.map((item) => (
              <button
                className="mobile-work-card"
                key={item.task_id}
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
          </div>
          {state.more && (
            <button
              disabled={state.loading}
              onClick={() => void controller.more()}
            >
              加载更多
            </button>
          )}
        </>
      )}
    </main>
  );
}

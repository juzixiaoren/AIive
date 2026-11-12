import { useState } from "react";
import type {
  WorkController,
  WorkState,
} from "../../../shared/work/controller";
import {
  actionLabels,
  attentionHelp,
  goalControls,
  performAttention,
  type PlanChanges,
} from "../../../shared/work/operations";
import {
  deviceLabel,
  statusLabel,
  type Attention,
} from "../../../shared/work/types";
import WorkFile from "./WorkFile";
import WorkPlanEditor from "./WorkPlanEditor";

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
  const input = item.kind === "goal_question" || item.kind === "task_question";
  return (
    <article className="work-attention">
      <h3>{item.title}</h3>
      <p>{item.description}</p>
      <p className="work-hint">{attentionHelp[item.kind]}</p>
      {item.expires_at && (
        <small>有效期至 {new Date(item.expires_at).toLocaleString()}</small>
      )}
      {item.preview && <pre>{JSON.stringify(item.preview, null, 2)}</pre>}
      {input && (
        <label>
          你的回答
          <textarea
            maxLength={2000}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
          />
        </label>
      )}
      <div className="work-buttons">
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
                !window.confirm("拒绝该操作会使当前工作失败，确认拒绝？")
              )
                return;
              if (
                action === "reject" &&
                !window.confirm(
                  item.kind === "goal_question"
                    ? "跳过当前步骤？目标仍会保留。"
                    : "拒绝整个目标计划？",
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

export default function WorkDetail({
  state,
  controller,
}: {
  state: WorkState;
  controller: WorkController;
}) {
  const [editor, setEditor] = useState<{
    item?: Attention;
    answer: string;
  } | null>(null);
  const view = state.view;
  if (!view)
    return <p>{state.loading ? "正在读取工作…" : "选择一项工作查看详情。"}</p>;
  const task = view.task,
    goal = view.goal;
  const save = (changes: PlanChanges) => {
    void controller
      .act(() =>
        editor?.item
          ? performAttention(
              controller.api,
              task.task_id,
              editor.item,
              editor.item.kind === "goal_question" ? "modify_plan" : "modify",
              editor.answer || "按修改后的计划继续",
              changes,
            )
          : controller.api.goal(task.task_id, "modify", changes),
      )
      .then((ok) => {
        if (ok) setEditor(null);
      });
  };
  return (
    <div className="work-detail">
      <header>
        <span className="work-eyebrow">
          {goal ? "长期目标" : "委派工作"} · {statusLabel(task.task_status)}
        </span>
        <h2>{task.title}</h2>
        <p>{task.goal}</p>
        <p className="work-hint">{deviceLabel(view.target_device)}</p>
        <small>更新于 {new Date(task.updated_at).toLocaleString()}</small>
      </header>
      {view.data_warning && (
        <p role="status">部分执行记录未能完整读取，请刷新或检查工作状态。</p>
      )}
      {task.last_error && <p role="alert">{task.last_error}</p>}
      {view.attention_items.length > 0 && (
        <section>
          <h2>待你处理</h2>
          {view.attention_items.map((item) => (
            <AttentionCard
              key={item.id}
              item={item}
              state={state}
              controller={controller}
              onEdit={(item, answer) => setEditor({ item, answer })}
            />
          ))}
        </section>
      )}
      {goal && (
        <section>
          <h2>目标计划 · {statusLabel(goal.status)}</h2>
          <p>{goal.summary}</p>
          <progress
            value={goal.progress.done}
            max={Math.max(1, goal.progress.total)}
            aria-label="目标步骤完成进度"
          />
          <p>
            {goal.progress.done} / {goal.progress.total} 步已完成 ·{" "}
            {goal.progress.waiting} 步等待 · {goal.progress.failed} 步失败
          </p>
          {goal.deadline && (
            <p>
              截止 {new Date(goal.deadline).toLocaleString()} · {goal.timezone}
            </p>
          )}
          {goal.blocked_reason && <p>{goal.blocked_reason}</p>}
          <ul>
            {goal.acceptance.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
          <ol className="work-steps">
            {goal.steps.map((step) => (
              <li key={step.id}>
                <strong>{step.title}</strong>{" "}
                <span>{statusLabel(step.status)}</span>
                <p>
                  {
                    (
                      {
                        autonomous: "自动执行",
                        interactive: "用户参与",
                        approval_required: "需要步骤授权",
                      } as Record<string, string>
                    )[step.kind]
                  }
                </p>
                {step.note && <p>{step.note}</p>}
                {step.next_trigger_at && (
                  <small>
                    下次触发 {new Date(step.next_trigger_at).toLocaleString()}
                  </small>
                )}
                <ul>
                  {step.acceptance.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
          <div className="work-buttons">
            {goalControls(goal.status).map((action) => (
              <button
                key={action}
                disabled={state.busy}
                onClick={() => {
                  if (action === "modify") {
                    setEditor({ answer: "" });
                    return;
                  }
                  if (
                    ["end", "reject"].includes(action) &&
                    !window.confirm(
                      action === "end"
                        ? "结束目标？已经启动的外部操作可能仍会完成。"
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
            <p className="work-hint">
              暂停不会保证立即中断运行中的操作；结束目标也不会撤销已启动的外部副作用。
            </p>
          )}
          {editor && (
            <WorkPlanEditor
              key={`${task.task_id}:${goal.revision}`}
              goal={goal}
              busy={state.busy}
              onSave={save}
              onCancel={() => setEditor(null)}
            />
          )}
        </section>
      )}
      {!goal && view.bucket !== "ended" && (
        <section>
          <button
            disabled={state.busy}
            onClick={() => {
              if (
                window.confirm("取消这项工作？已启动的外部操作可能仍会完成。")
              )
                void controller.act(() => controller.api.cancel(task.task_id));
            }}
          >
            取消工作
          </button>
          <p className="work-hint">
            取消不能保证已经启动的外部副作用立即停止。
          </p>
        </section>
      )}
      <section>
        <h2>执行时间线</h2>
        {state.timeline.length === 0 && <p>暂无可见执行事件。</p>}
        <ol className="work-timeline">
          {state.timeline.map((event) => (
            <li key={event.sequence}>
              <strong>{event.title}</strong>
              <time>{new Date(event.created_at).toLocaleString()}</time>
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
          <article className="work-report">
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
            api={controller.api}
            task={task.task_id}
            file={file}
            kind="artifacts"
          />
        ))}
        {view.evidence.map((file) => (
          <WorkFile
            key={file.id}
            api={controller.api}
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
            <strong>{action.capability_id}</strong>
            <p>
              {statusLabel(action.status)} · 风险 {action.risk_level}
            </p>
            <small>
              {action.id} · {new Date(action.created_at).toLocaleString()}
            </small>
          </article>
        ))}
      </details>
    </div>
  );
}

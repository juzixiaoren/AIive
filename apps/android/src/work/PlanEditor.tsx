import { useState } from "react";
import type { Goal } from "../../../../shared/work/types";
import {
  editablePlan,
  validatePlan,
  type PlanChanges,
} from "../../../../shared/work/operations";

export default function PlanEditor({
  goal,
  busy,
  onSave,
  onClose,
}: {
  goal: Goal;
  busy: boolean;
  onSave: (plan: PlanChanges) => void;
  onClose: () => void;
}) {
  const [plan, setPlan] = useState(() => editablePlan(goal)),
    [error, setError] = useState("");
  return (
    <form
      className="mobile-editor"
      onSubmit={(e) => {
        e.preventDefault();
        try {
          validatePlan(plan);
          setError("");
          onSave(plan);
        } catch (cause) {
          setError((cause as Error).message);
        }
      }}
    >
      <h2>修改计划</h2>
      <p>
        修改执行内容会重置该步骤及依赖它的后续步骤。正在执行的操作结束后才能修改。
      </p>
      <label>
        目标
        <textarea
          required
          maxLength={2000}
          value={plan.summary}
          onChange={(e) => setPlan({ ...plan, summary: e.target.value })}
        />
      </label>
      <label>
        截止时间（含时区，可留空）
        <input
          placeholder="2026-12-31T18:00:00+08:00"
          value={plan.deadline || ""}
          onChange={(e) => setPlan({ ...plan, deadline: e.target.value })}
        />
      </label>
      <label>
        时区
        <input
          value={plan.timezone}
          onChange={(e) => setPlan({ ...plan, timezone: e.target.value })}
        />
      </label>
      <label>
        验收标准（每行一项）
        <textarea
          value={plan.acceptance.join("\n")}
          onChange={(e) =>
            setPlan({ ...plan, acceptance: e.target.value.split("\n") })
          }
        />
      </label>
      {plan.steps.map((step, index) => {
        const update = (patch: Partial<typeof step>) =>
          setPlan({
            ...plan,
            steps: plan.steps.map((s) =>
              s.id === step.id ? { ...s, ...patch } : s,
            ),
          });
        return (
          <fieldset key={step.id}>
            <legend>步骤 {index + 1}</legend>
            <label>
              内容
              <input
                required
                value={step.title}
                onChange={(e) => update({ title: e.target.value })}
              />
            </label>
            <label>
              执行方式
              <select
                value={step.kind}
                onChange={(e) => update({ kind: e.target.value })}
              >
                <option value="autonomous">自动执行</option>
                <option value="interactive">用户参与</option>
                <option value="approval_required">步骤审批</option>
              </select>
            </label>
            <label>
              步骤验收标准
              <textarea
                value={step.acceptance.join("\n")}
                onChange={(e) =>
                  update({ acceptance: e.target.value.split("\n") })
                }
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={step.requires_evidence}
                onChange={(e) =>
                  update({ requires_evidence: e.target.checked })
                }
              />
              需要执行证据
            </label>
            <p>依赖步骤</p>
            {plan.steps
              .filter((s) => s.id !== step.id)
              .map((dep) => (
                <label key={dep.id}>
                  <input
                    type="checkbox"
                    checked={step.depends_on.includes(dep.id)}
                    onChange={(e) =>
                      update({
                        depends_on: e.target.checked
                          ? [...step.depends_on, dep.id]
                          : step.depends_on.filter((id) => id !== dep.id),
                      })
                    }
                  />
                  {dep.title}
                </label>
              ))}
            <button
              type="button"
              onClick={() =>
                setPlan({
                  ...plan,
                  steps: plan.steps
                    .filter((s) => s.id !== step.id)
                    .map((s) => ({
                      ...s,
                      depends_on: s.depends_on.filter((id) => id !== step.id),
                    })),
                })
              }
            >
              移除此步骤
            </button>
          </fieldset>
        );
      })}
      <button
        type="button"
        onClick={() =>
          setPlan({
            ...plan,
            steps: [
              ...plan.steps,
              {
                id: crypto.randomUUID(),
                title: "",
                kind: "autonomous",
                depends_on: [],
                acceptance: [],
                requires_evidence: false,
              },
            ],
          })
        }
      >
        添加步骤
      </button>
      {error && <p role="alert">{error}</p>}
      <div className="mobile-actions">
        <button type="submit" disabled={busy}>
          保存修改
        </button>
        <button type="button" onClick={onClose}>
          取消编辑
        </button>
      </div>
    </form>
  );
}

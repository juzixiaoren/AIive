import { useState } from "react";
import type { Goal } from "../../../shared/work/types";
import {
  editablePlan,
  validatePlan,
  type PlanChanges,
} from "../../../shared/work/operations";

export default function WorkPlanEditor({
  goal,
  busy,
  onSave,
  onCancel,
}: {
  goal: Goal;
  busy: boolean;
  onSave: (changes: PlanChanges) => void;
  onCancel: () => void;
}) {
  const [plan, setPlan] = useState(() => editablePlan(goal));
  const [error, setError] = useState("");
  return (
    <form
      className="work-editor"
      onSubmit={(event) => {
        event.preventDefault();
        try {
          validatePlan(plan);
          setError("");
          onSave(plan);
        } catch (e) {
          setError((e as Error).message);
        }
      }}
    >
      <h3>修改目标计划</h3>
      <p>
        内容修改会使当前步骤和依赖它的后续步骤重新执行。正在运行的操作需先结束，才能修改。
      </p>
      <label>
        目标说明
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
      {plan.steps.map((step, index) => (
        <fieldset key={step.id}>
          <legend>步骤 {index + 1}</legend>
          <label>
            执行内容
            <input
              required
              value={step.title}
              onChange={(e) =>
                setPlan({
                  ...plan,
                  steps: plan.steps.map((s) =>
                    s.id === step.id ? { ...s, title: e.target.value } : s,
                  ),
                })
              }
            />
          </label>
          <label>
            执行方式
            <select
              value={step.kind}
              onChange={(e) =>
                setPlan({
                  ...plan,
                  steps: plan.steps.map((s) =>
                    s.id === step.id ? { ...s, kind: e.target.value } : s,
                  ),
                })
              }
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
                setPlan({
                  ...plan,
                  steps: plan.steps.map((s) =>
                    s.id === step.id
                      ? { ...s, acceptance: e.target.value.split("\n") }
                      : s,
                  ),
                })
              }
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={step.requires_evidence}
              onChange={(e) =>
                setPlan({
                  ...plan,
                  steps: plan.steps.map((s) =>
                    s.id === step.id
                      ? { ...s, requires_evidence: e.target.checked }
                      : s,
                  ),
                })
              }
            />
            需要执行证据
          </label>
          <div>
            依赖步骤
            {plan.steps
              .filter((s) => s.id !== step.id)
              .map((dep) => (
                <label key={dep.id}>
                  <input
                    type="checkbox"
                    checked={step.depends_on.includes(dep.id)}
                    onChange={(e) =>
                      setPlan({
                        ...plan,
                        steps: plan.steps.map((s) =>
                          s.id === step.id
                            ? {
                                ...s,
                                depends_on: e.target.checked
                                  ? [...s.depends_on, dep.id]
                                  : s.depends_on.filter((id) => id !== dep.id),
                              }
                            : s,
                        ),
                      })
                    }
                  />
                  {dep.title}
                </label>
              ))}
          </div>
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
      ))}
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
                acceptance: [],
                depends_on: [],
                requires_evidence: false,
              },
            ],
          })
        }
      >
        添加步骤
      </button>
      {error && <p role="alert">{error}</p>}
      <div className="work-buttons">
        <button disabled={busy} type="submit">
          保存计划
        </button>
        <button type="button" onClick={onCancel}>
          取消编辑
        </button>
      </div>
    </form>
  );
}

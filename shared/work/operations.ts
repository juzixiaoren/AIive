import type {
  Attention,
  Goal,
  GoalControl,
  GoalStep,
  QuestionIntent,
} from "./types";
import type { WorkClient } from "./client";

export const actionLabels: Record<string, string> = {
  approve: "批准操作",
  deny: "拒绝操作",
  send_input: "提交补充信息",
  confirm: "确认计划",
  modify: "修改计划",
  reject: "拒绝",
  answer: "回答问题",
  modify_plan: "修改计划后回答",
  start_step: "开始此步骤",
  approve_step: "授权此步骤",
  end: "结束目标",
};
export const attentionHelp: Record<string, string> = {
  action_approval:
    "这是一次具体操作的审批。拒绝会使当前工作失败；批准的仅是服务端保存的操作。",
  goal_step_approval: "授权这个 Goal 步骤；具体高风险操作仍可能单独请求审批。",
  task_question: "补充信息只解除普通工作的用户输入阻塞。",
  goal_question: "回答将继续当前步骤；跳过只影响当前步骤，不会拒绝整个目标。",
  goal_confirmation: "确认后才开始执行。你也可以先修改计划或拒绝整个目标。",
  goal_step_start: "准备好后开始这个交互步骤。",
  goal_blocked: "检查计划与执行结果，修改计划后继续，或结束目标。",
};
export type PlanChanges = {
  summary: string;
  deadline: string | null;
  timezone: string;
  acceptance: string[];
  steps: Array<
    Pick<
      GoalStep,
      | "id"
      | "title"
      | "kind"
      | "acceptance"
      | "depends_on"
      | "requires_evidence"
    >
  >;
};
export function editablePlan(goal: Goal): PlanChanges {
  return {
    summary: goal.summary,
    deadline: goal.deadline || "",
    timezone: goal.timezone,
    acceptance: [...goal.acceptance],
    steps: goal.steps.map((s) => ({
      id: s.id,
      title: s.title,
      kind: s.kind,
      acceptance: [...s.acceptance],
      depends_on: [...s.depends_on],
      requires_evidence: s.requires_evidence,
    })),
  };
}
export function validatePlan(plan: PlanChanges): void {
  if (
    !plan.summary.trim() ||
    !plan.steps.length ||
    plan.steps.some((s) => !s.title.trim())
  )
    throw new Error("请填写目标和每个步骤的说明。");
  if (plan.deadline && Number.isNaN(Date.parse(plan.deadline)))
    throw new Error("截止时间格式无效。");
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: plan.timezone });
  } catch {
    throw new Error("时区无效，例如 Asia/Shanghai。");
  }
  const byId = new Map(plan.steps.map((s) => [s.id, s]));
  const visited = new Set<string>(),
    visiting = new Set<string>();
  const visit = (id: string) => {
    if (visiting.has(id)) throw new Error("步骤依赖不能形成循环。");
    if (visited.has(id)) return;
    const step = byId.get(id);
    if (!step) throw new Error("依赖的步骤不存在。");
    visiting.add(id);
    step.depends_on.forEach(visit);
    visiting.delete(id);
    visited.add(id);
  };
  plan.steps.forEach((s) => visit(s.id));
}
/** Kind is part of the dispatch key: "reject" on a question must never reject its Goal. */
export function performAttention(
  api: WorkClient,
  task: string,
  item: Attention,
  action: string,
  text = "",
  changes?: PlanChanges,
): Promise<unknown> {
  if (!item.allowed_actions.includes(action))
    throw new Error("操作已不可用，请刷新。");
  if (
    item.kind === "action_approval" &&
    item.approval_id &&
    (action === "approve" || action === "deny")
  )
    return api.approval(item.approval_id, action);
  if (item.kind === "task_question" && action === "send_input")
    return api.input(task, text);
  if (
    item.kind === "goal_question" &&
    item.step_id &&
    ["answer", "modify_plan", "reject"].includes(action)
  )
    return api.answer(
      task,
      item.step_id,
      item.question_id,
      text || "跳过当前步骤",
      action as QuestionIntent,
      changes,
    );
  if (
    item.kind === "goal_step_start" &&
    item.step_id &&
    action === "start_step"
  )
    return api.step(task, item.step_id, "start");
  if (
    item.kind === "goal_step_approval" &&
    item.step_id &&
    action === "approve_step"
  )
    return api.step(task, item.step_id, "approve");
  if (
    ["goal_confirmation", "goal_blocked"].includes(item.kind) &&
    ["confirm", "modify", "reject", "end"].includes(action)
  )
    return api.goal(
      task,
      action as GoalControl,
      action === "modify" ? changes : { reason: text },
    );
  throw new Error("不支持的操作，请刷新工作详情。");
}
export function goalControls(status: string): GoalControl[] {
  if (["completed", "rejected", "abandoned"].includes(status)) return [];
  return [
    ...(status === "proposed" ? (["confirm", "reject"] as GoalControl[]) : []),
    "modify",
    ...(status === "paused"
      ? (["resume"] as GoalControl[])
      : ["active", "blocked"].includes(status)
        ? (["pause"] as GoalControl[])
        : []),
    ...(status === "blocked" || status === "paused"
      ? (["reject"] as GoalControl[])
      : []),
    "end",
  ];
}

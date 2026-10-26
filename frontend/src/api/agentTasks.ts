export interface AgentTaskEvent {
  id: string;
  sequence: number;
  event_type: string;
  visibility: "conversation" | "task" | "internal";
  run_id?: string;
  action_id?: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface AgentTaskAction {
  id: string;
  sequence: number;
  run_id: string;
  execution_run_id?: string;
  capability_id: string;
  status: string;
  executor_type: string;
  risk_level: string;
  requires_approval: boolean;
  arguments: Record<string, unknown>;
  result_summary?: Record<string, unknown>;
  evidence_refs: Array<Record<string, unknown>>;
  error?: string;
  created_at: string;
  completed_at?: string;
}

export interface AgentTaskDetail {
  id: string;
  thread_id: string;
  task_type: string;
  title: string;
  goal: string;
  status: string;
  target_node_id?: string;
  task_brief: Record<string, unknown>;
  task_state: Record<string, unknown>;
  budgets: Record<string, unknown>;
  usage: Record<string, number>;
  report?: { status: string; summary: string; unresolved?: string[] };
  last_error?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
  runs: Array<Record<string, unknown>>;
  actions: AgentTaskAction[];
  events?: AgentTaskEvent[];
  evidence: Array<Record<string, unknown>>;
  artifacts: Array<Record<string, unknown>>;
  approvals: Array<{
    id: string;
    action_id: string;
    tool_name: string;
    status: string;
    risk_snapshot: Record<string, unknown>;
    preconditions: Record<string, unknown>;
    effects: Record<string, unknown>;
    expires_at?: string;
  }>;
}

export interface AgentTaskSummary {
  id: string;
  thread_id: string;
  task_type: string;
  title: string;
  goal: string;
  status: string;
  executor_type: string;
  target_node_id?: string;
  usage: Record<string, number>;
  report?: { status: string; summary: string; unresolved?: string[] };
  last_error?: string;
  action_count: number;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

async function json<T>(pending: Response | Promise<Response>): Promise<T> {
  const response = await pending;
  if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

export async function listAgentTasks(threadId?: string): Promise<AgentTaskSummary[]> {
  const params = new URLSearchParams();
  if (threadId) params.set("thread_id", threadId);
  return json(fetch(`api/agent-tasks${params.size ? `?${params}` : ""}`));
}

export async function getAgentTask(taskId: string): Promise<AgentTaskDetail> {
  return json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}`));
}

export async function createAgentTask(input: {
  thread_id: string;
  goal: string;
  title?: string;
  task_type?: string;
  target_node_id?: string;
  task_brief?: Record<string, unknown>;
}): Promise<AgentTaskDetail> {
  return json(fetch("api/agent-tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function cancelAgentTask(taskId: string): Promise<void> {
  await json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }));
}

export async function sendAgentTaskInput(taskId: string, content: string): Promise<void> {
  await json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/input`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  }));
}

export async function decideTaskApproval(approvalId: string, action: "approve" | "deny"): Promise<void> {
  await json(fetch("api/approval/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, action }),
  }));
}


export interface AgentTaskGoalStep {
  id: string;
  title: string;
  kind: string;
  status: string;
  depends_on: string[];
  acceptance: string[];
  schedule?: Record<string, unknown> | null;
  requires_evidence: boolean;
  evidence_refs: string[];
  attempts: number;
  missed_count: number;
  next_trigger_at?: string | null;
  last_result?: Record<string, unknown> | null;
  note: string;
  approved_at?: string | null;
}

export interface AgentTaskGoal {
  task_id: string;
  thread_id: string;
  task_status: string;
  plan: {
    status: string;
    summary: string;
    deadline?: string | null;
    timezone: string;
    acceptance: string[];
    steps: AgentTaskGoalStep[];
    current_step_id?: string | null;
    revision: number;
    blocked_reason: string;
  };
  summary: {
    status: string;
    summary: string;
    deadline?: string | null;
    progress: { total: number; done: number; failed: number; pending: number; waiting: number };
    current_step?: { id: string; title: string; kind: string; status: string } | null;
    next_trigger_at?: string | null;
    blocked_reason: string;
    steps: Array<{ id: string; title: string; kind: string; status: string }>;
  };
  next_wake_at?: string | null;
}

export async function getAgentTaskGoal(taskId: string): Promise<AgentTaskGoal> {
  return json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/goal`));
}

export async function controlAgentTaskGoal(
  taskId: string,
  action: "confirm" | "reject" | "pause" | "resume" | "end" | "modify",
  body?: Record<string, unknown>,
): Promise<AgentTaskGoal> {
  return json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/goal/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }));
}

export async function approveAgentTaskGoalStep(taskId: string, stepId: string): Promise<AgentTaskGoal> {
  return json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/goal/steps/${encodeURIComponent(stepId)}/approve`, { method: "POST" }));
}

export async function startAgentTaskGoalStep(taskId: string, stepId: string): Promise<AgentTaskGoal> {
  return json(fetch(`api/agent-tasks/${encodeURIComponent(taskId)}/goal/steps/${encodeURIComponent(stepId)}/start`, { method: "POST" }));
}

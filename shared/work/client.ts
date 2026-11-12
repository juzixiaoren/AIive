import type {
  Bucket,
  Fetcher,
  GoalControl,
  Inbox,
  QuestionIntent,
  Timeline,
  WorkView,
} from "./types";

export class WorkApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
const errors: Record<string, string> = {
  goal_running: "当前步骤仍在执行。暂停后请等待操作结束再修改计划。",
  question_expired: "问题已过期，请刷新工作详情。",
  question_not_found: "问题已处理或不再可见，请刷新。",
  goal_terminal: "目标已结束，不能修改。",
  stale_question: "问题已变化，请刷新后重试。",
};
export class WorkClient {
  constructor(readonly fetch: Fetcher) {}
  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await this.fetch(path, init);
    if (!res.ok) {
      let detail = "";
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : "";
      } catch {
        /* use status */
      }
      throw new WorkApiError(
        res.status,
        errors[detail] ||
          (res.status === 404
            ? "工作或内容不存在，或已不可见。"
            : `操作未完成（${res.status}），请刷新后重试。`),
      );
    }
    return res.json() as Promise<T>;
  }
  private path(id: string): string {
    return `agent-tasks/${encodeURIComponent(id)}`;
  }
  currentThread(): Promise<{ thread_id: string }> {
    return this.json("conversation/current");
  }
  inbox(bucket: Bucket, cursor?: string): Promise<Inbox> {
    const query = new URLSearchParams({ bucket, limit: "25" });
    if (cursor) query.set("cursor", cursor);
    return this.json(`agent-tasks/inbox?${query}`);
  }
  view(id: string): Promise<WorkView> {
    return this.json(`${this.path(id)}/work-view`);
  }
  timeline(id: string, after: number): Promise<Timeline> {
    return this.json(
      `${this.path(id)}/timeline?after_sequence=${after}&limit=100`,
    );
  }
  private post(path: string, body: unknown = {}): Promise<unknown> {
    return this.json(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }
  input(id: string, content: string): Promise<unknown> {
    return this.post(`${this.path(id)}/input`, { content });
  }
  cancel(id: string): Promise<unknown> {
    return this.post(`${this.path(id)}/cancel`);
  }
  approval(approval_id: string, action: "approve" | "deny"): Promise<unknown> {
    return this.post("approval/respond", { approval_id, action });
  }
  goal(id: string, action: GoalControl, body?: unknown): Promise<unknown> {
    return this.post(`${this.path(id)}/goal/${action}`, body);
  }
  step(
    id: string,
    step: string,
    action: "start" | "approve",
  ): Promise<unknown> {
    return this.post(
      `${this.path(id)}/goal/steps/${encodeURIComponent(step)}/${action}`,
    );
  }
  answer(
    id: string,
    step_id: string,
    question_id: string | null | undefined,
    answer: string,
    intent: QuestionIntent,
    plan_changes?: unknown,
  ): Promise<unknown> {
    return this.post(`${this.path(id)}/goal/questions/answer`, {
      step_id,
      question_id: question_id || null,
      answer,
      intent,
      plan_changes,
    });
  }
}

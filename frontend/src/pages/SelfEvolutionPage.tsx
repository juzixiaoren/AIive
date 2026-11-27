import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { BASE_PATH } from "../lib/base";

type Section = "episodes" | "candidates" | "insights";
type Use = { id: string; version: number | string; kind: string };
type Evidence = { episode_id: string; source_id: string; role?: string; outcome?: string };
type Episode = {
  id: string;
  kind: "turn" | "task";
  family: string;
  source_id: string;
  parent_id?: string;
  title?: string;
  result_preview?: string;
  result_truncated?: boolean;
  outcome: string;
  completed_at: string;
  feedback?: string;
  uses: Use[];
  metrics: Record<string, number | null>;
  signals?: { kind: string; source_id: string; created_at: string; method?: string }[];
  refs?: Record<string, string | string[] | null>;
};
type Measure = { value: number | null; status: string; numerator?: number; denominator?: number; samples?: number };
type Cohort = {
  samples: number;
  follow_up_samples: number;
  skill_exposures: number;
  rates: Record<string, Measure>;
  medians: Record<string, Measure>;
};
type Effect = {
  status: string;
  minimum_samples: number;
  windows?: Record<string, { start: string; end: string }>;
  cohorts: Record<string, Record<string, Cohort>>;
};
type Insight = {
  id: string;
  insight_id: string;
  version: number;
  kind: string;
  family: string;
  title: string;
  status: string;
  operation: string;
  support_count: number;
  refute_count: number;
  body?: Record<string, string | string[]>;
  evidence?: Evidence[];
  usage?: { samples: number; helpful: number; harmful: number };
  effect?: Effect;
  eligible?: boolean;
};
type Candidate = {
  candidate_id: string;
  title: string;
  status: string;
  route: string;
  problem: string;
  target: string;
  preference?: string;
  occurrences: number;
  evidence_ids: string[];
  evidence_groups?: { signature?: string; category?: string; occurrences?: number }[];
  experience_refs?: Evidence[];
  acceptance_criteria: string[];
  test_method: string;
  progress: { kind?: string; id?: string; status?: string; repair_status?: string; selected?: string };
};
type EpisodePage = { items: Episode[]; next_cursor: string | null };
type Home = { episodes: Episode[]; episodes_next_cursor: string | null; insights: Insight[]; candidates: Candidate[] };
type Selection = { section: "episodes"; row: Episode } | { section: "insights"; row: Insight } |
  { section: "candidates"; row: Candidate };

const labels: Record<string, string> = {
  episodes: "经历与反馈", candidates: "改进候选", insights: "经验库",
  turn: "对话", task: "任务", playbook: "Playbook", skill: "Skill", tool_hint: "工具提示",
  code: "代码修复", mcp: "MCP 能力", memory: "用户偏好",
  general: "通用", chat: "对话", coding: "编程", document: "文档", knowledge: "知识库",
  web_research: "联网研究", selfdev: "代码修复",
  unknown: "结果未知", verified_success: "已验证成功", agent_reported_success: "Agent 报告成功",
  explicit_positive: "明确有帮助", explicit_negative: "需要改进", corrected: "已纠正", rework: "返工",
  partial: "部分完成", failed: "失败", succeeded: "成功", queued: "排队中", running: "执行中",
  tests_passed: "补丁验证通过", dispatched: "已派发", awaiting_approval: "等待审批",
  new_request: "新需求", continuation: "继续讨论", execution_efficiency: "执行效率",
  search_no_results: "工具搜索无结果", capability_missing_from_catalog: "目录缺少能力",
  capability_out_of_scope: "能力超出授权范围", discovery_temporary_failure: "检索暂时不可用",
  pending: "待审阅", active: "已启用", disabled: "已停用", superseded: "已有新版本",
  deprecated: "已弃用", rejected: "已拒绝", candidate_pending: "待审阅", candidate_accepted: "已接受",
  candidate_rejected: "已拒绝", evaluated: "已评估", activated: "已激活", written: "已写入",
  conflict_pending: "等待偏好确认",
  conditions: "适用条件", match_terms: "匹配词", steps: "步骤", required_capabilities: "所需能力",
  expected_result: "预期结果", verification: "验证", recovery: "失败恢复", root_cause: "根因",
  correction: "用户纠正率", task_failure: "任务失败率", tool_failure: "工具失败率",
  skill_success: "Skill 成功率", skill_rework: "Skill 返工率",
  actions: "动作数", tokens: "Token 数", duration_ms: "耗时（毫秒）",
  before: "启用前", after: "启用后全部", exposed: "启用后实际使用",
  support: "支持", refute: "反例", comparison: "对照", discovery: "工具发现",
  ADD: "新增", EDIT: "修改", UPVOTE: "支持", DOWNVOTE: "反驳", DEPRECATE: "弃用",
};

const label = (key: string) => labels[key] ?? key;
const time = (value: string) => new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : value + "Z").toLocaleString();
const button = "rounded-md border border-divider px-3 py-1.5 text-sm text-content hover:bg-surface-muted disabled:opacity-50";
const card = "rounded-xl border border-divider bg-surface p-4";

async function request<T>(path = "", body?: object): Promise<T> {
  const response = await apiFetch(`${BASE_PATH}api/experience${path}`, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `请求失败（${response.status}）`);
  }
  return response.json();
}

function Effects({ effect }: { effect: Effect }) {
  return <section className="space-y-3">
    <h3 className="font-semibold text-title">使用效果</h3>
    <p className="text-sm text-muted">
      {effect.status === "insufficient_data" ? `样本不足（每组至少 ${effect.minimum_samples} 条）` : "已获得可比较样本"}
      {" · 同任务族观察数据，无法单独归因于经验版本。"}
    </p>
    {effect.windows && <div className="text-xs text-muted">
      {Object.entries(effect.windows).map(([key, window]) =>
        <p key={key}>{label(key)} {time(window.start)} 至 {time(window.end)}</p>)}
    </div>}
    {Object.entries(effect.cohorts).map(([kind, groups]) => <details key={kind} className="rounded-md bg-surface-muted p-3" open={Object.values(groups).some(group => group.samples > 0)}>
      <summary className="cursor-pointer text-sm font-medium">{label(kind)}样本</summary>
      <div className="overflow-x-auto pt-3">
        <table className="w-full min-w-[28rem] text-left text-xs">
          <thead><tr><th scope="col">观测指标</th>{Object.keys(groups).map(key => <th className="px-2" scope="col" key={key}>{label(key)}</th>)}</tr></thead>
          <tbody>
            <tr><th scope="row">经历数</th>{Object.entries(groups).map(([key, group]) => <td className="p-2" key={key}>{group.samples}（反馈 {group.follow_up_samples}）</td>)}</tr>
            {Object.keys({ ...groups.before?.rates, ...groups.before?.medians }).map(metric => <tr key={metric} className="border-t border-divider">
              <th className="py-2 font-normal" scope="row">{label(metric)}</th>
              {Object.entries(groups).map(([key, group]) => {
                const value = group.rates[metric] ?? group.medians[metric];
                const rate = value.denominator !== undefined;
                const sample = rate ? `${value.numerator}/${value.denominator}` : `n=${value.samples}`;
                return <td className="p-2" key={key}>{value.value === null ? "样本不足" : rate ? `${(value.value * 100).toFixed(1)}%` : value.value.toLocaleString()} · {sample}</td>;
              })}
            </tr>)}
          </tbody>
        </table>
      </div>
    </details>)}
  </section>;
}

export default function SelfEvolutionPage({ onOpenWork }: { onOpenWork: (id: string) => void }) {
  const [section, setSection] = useState<Section>("episodes");
  const [home, setHome] = useState<Home>();
  const [detail, setDetail] = useState<Selection>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const listSequence = useRef(0);
  const [listBusy, setListBusy] = useState(false);
  const detailHeading = useRef<HTMLHeadingElement>(null);

  const refresh = useCallback(async () => {
    const current = ++listSequence.current;
    setListBusy(true);
    setError("");
    try {
      const result = await request<Home>();
      if (current === listSequence.current) setHome(result);
    } catch (err) {
      if (current === listSequence.current) setError((err as Error).message);
    } finally {
      if (current === listSequence.current) setListBusy(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  async function loadOlder() {
    const cursor = home?.episodes_next_cursor;
    if (!cursor || listBusy) return;
    const current = ++listSequence.current;
    setListBusy(true);
    setError("");
    try {
      const page = await request<EpisodePage>(`/episodes?cursor=${encodeURIComponent(cursor)}`);
      if (current === listSequence.current) {
        setHome(prev => prev?.episodes_next_cursor === cursor
          ? { ...prev, episodes: [...prev.episodes, ...page.items], episodes_next_cursor: page.next_cursor }
          : prev);
      }
    } catch (err) {
      if (current === listSequence.current) setError((err as Error).message);
    } finally {
      if (current === listSequence.current) setListBusy(false);
    }
  }

  async function open(next: Section, id: string) {
    const current = ++sequence.current;
    setBusy(true);
    setError("");
    try {
      const path = `/${next}/${id}`;
      const result: Selection = next === "episodes" ? { section: next, row: await request<Episode>(path) } :
        next === "insights" ? { section: next, row: await request<Insight>(path) } :
          { section: next, row: await request<Candidate>(path) };
      if (current === sequence.current) {
        setSection(next);
        setDetail(result);
      }
    } catch (err) {
      if (current === sequence.current) setError((err as Error).message);
    } finally {
      if (current === sequence.current) setBusy(false);
    }
  }

  useEffect(() => { if (detail) detailHeading.current?.focus(); }, [detail]);

  async function mutate(action: string, body: object = {}) {
    if (!detail) return;
    setBusy(true);
    setError("");
    const current = ++sequence.current;
    const id = detail.section === "candidates" ? detail.row.candidate_id : detail.row.id;
    try {
      const path = `/${detail.section}/${id}/${action}`;
      const result: Selection = detail.section === "episodes" ? { section: "episodes", row: await request<Episode>(path, body) } :
        detail.section === "insights" ? { section: "insights", row: await request<Insight>(path, body) } :
          { section: "candidates", row: await request<Candidate>(path, body) };
      if (current === sequence.current) setDetail(result);
      if (result.section === "episodes") {
        setHome(prev => prev && { ...prev, episodes: prev.episodes.map(
          row => row.id === result.row.id ? result.row : row,
        ) });
      } else {
        await refresh();
      }
    } catch (err) {
      if (current === sequence.current) setError((err as Error).message);
    } finally {
      if (current === sequence.current) setBusy(false);
    }
  }

  function evidenceList(items: Evidence[]) {
    return <ul className="mt-2 space-y-1 text-sm">
      {items.map(item => <li key={`${item.episode_id}-${item.source_id}`} className="flex flex-wrap items-center gap-2">
        <button className="text-content underline underline-offset-2" onClick={() => void open("episodes", item.episode_id)}>查看经历</button>
        <span className="text-muted">{[item.role, item.outcome].flatMap(key => key ? [label(key)] : []).join(" · ")}</span>
        <code className="break-all text-xs text-muted">{item.source_id}</code>
      </li>)}
    </ul>;
  }

  return <div className="space-y-5 pb-10 text-content">
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-title">自进化中心</h2>
        <p className="mt-1 text-sm text-muted">查看实际经历、审阅改进，并追踪启用后的效果。</p>
      </div>
      <button type="button" className={button} disabled={busy || listBusy} onClick={() => detail ?
        void open(detail.section, detail.section === "candidates" ? detail.row.candidate_id : detail.row.id) : void refresh()}>刷新</button>
    </header>
    <nav aria-label="自进化中心" className="flex flex-wrap gap-2 border-b border-divider pb-3">
      {(["episodes", "candidates", "insights"] as const).map(next => <button type="button" key={next}
        aria-current={next === section ? "page" : undefined}
        className={`${button} ${next === section ? "bg-surface-muted font-semibold" : ""}`}
        onClick={() => { sequence.current++; setBusy(false); setSection(next); setDetail(undefined); setError(""); }}>
        {label(next)}
      </button>)}
    </nav>
    {error && <p role="alert" className="rounded-md border border-divider p-3 text-sm">{error}</p>}
    {!home && !error && <p role="status" className="text-sm text-muted">加载中…</p>}
    {detail ? <article className={`${card} space-y-4`}>
      <button type="button" className={button} onClick={() => { sequence.current++; setBusy(false); setDetail(undefined); }}>返回列表</button>
      <h3 ref={detailHeading} tabIndex={-1} className="break-words text-lg font-semibold text-title">
        {detail.row.title ?? (detail.section === "episodes" ? `${label(detail.row.kind)}经历` : "")}
      </h3>
      {detail.section === "episodes" && <>
        <p className="text-sm text-muted">{label(detail.row.family)} · {label(detail.row.outcome)} · {time(detail.row.completed_at)}</p>
        {detail.row.result_preview && <section>
          <h4 className="font-medium">{detail.row.kind === "turn" ? "当时的答复" : "任务报告"}</h4>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm">{detail.row.result_preview}</p>
          {detail.row.result_truncated && <p className="text-xs text-muted">已显示前 2000 字</p>}
        </section>}
        <div className="flex flex-wrap gap-2">
          {(["explicit_positive", "explicit_negative", "rework"] as const).map(kind => <button type="button" key={kind}
            aria-pressed={detail.row.feedback === kind} disabled={busy}
            className={`${button} ${detail.row.feedback === kind ? "bg-surface-muted" : ""}`}
            onClick={() => void mutate("feedback", { kind })}>{label(kind)}</button>)}
          {detail.row.kind === "task" && <button className={button} onClick={() => onOpenWork(detail.row.source_id)}>打开任务</button>}
          {detail.row.parent_id && <button className={button} onClick={() => void open("episodes", detail.row.parent_id!)}>查看父经历</button>}
        </div>
        {detail.row.uses.length > 0 && <section><h4 className="font-medium">使用的经验版本</h4>
          <ul className="text-sm text-muted">{detail.row.uses.map(use => <li key={`${use.id}-${use.version}`}>{label(use.kind)} · {use.id} · v{use.version}</li>)}</ul>
        </section>}
        <section><h4 className="font-medium">反馈与运行信号</h4>
          <ul className="mt-2 space-y-1 text-sm">{detail.row.signals?.map(signal =>
            <li key={`${signal.kind}-${signal.source_id}`}>{label(signal.kind)} · {time(signal.created_at)}</li>)}</ul>
        </section>
        <details><summary className="cursor-pointer text-sm">执行与上下文引用</summary>
          <dl className="mt-2 space-y-2 text-xs">{Object.entries(detail.row.refs ?? {}).filter(([, value]) => value && (!Array.isArray(value) || value.length)).map(([key, value]) =>
            <div key={key} className="break-all"><dt className="font-medium">{key}</dt><dd className="text-muted">{Array.isArray(value) ? value.join(", ") : String(value)}</dd></div>)}</dl>
        </details>
      </>}
      {detail.section === "candidates" && <>
        <p className="text-sm text-muted">{label(detail.row.route)} · {label(detail.row.status)} · {detail.row.occurrences} 条证据</p>
        <p className="text-sm">{detail.row.problem}</p>
        {(detail.row.preference || detail.row.target) && <p className="whitespace-pre-wrap text-sm">{detail.row.preference || detail.row.target}</p>}
        {detail.row.acceptance_criteria.length > 0 && <section><h4 className="font-medium">验收标准</h4>
          <ul className="list-disc pl-5 text-sm">{detail.row.acceptance_criteria.map(item => <li key={item}>{item}</li>)}</ul>
          {detail.row.test_method && <p className="mt-2 text-sm text-muted">{detail.row.test_method}</p>}
        </section>}
        <section><h4 className="font-medium">来源证据</h4>
          {detail.row.experience_refs?.length ? evidenceList(detail.row.experience_refs) :
            <ul className="mt-2 space-y-1 break-all text-xs">{detail.row.evidence_ids.map(id => <li key={id}>{id}</li>)}</ul>}
        </section>
        {detail.row.progress.status && <section className="space-y-2">
          <h4 className="font-medium">处理进度</h4>
          <p className="text-sm">{label(detail.row.progress.status)} {detail.row.progress.repair_status && `· ${label(detail.row.progress.repair_status)}`} {detail.row.progress.selected}</p>
          {detail.row.progress.kind === "selfdev" && detail.row.progress.id &&
            <button className={button} onClick={() => onOpenWork(detail.row.progress.id!)}>打开修复任务</button>}
        </section>}
        {detail.row.status === "candidate_pending" && <div className="flex gap-2">
          <button className={button} disabled={busy} onClick={() => void mutate("accept")}>接受</button>
          <button className={button} disabled={busy} onClick={() => void mutate("reject")}>拒绝</button>
        </div>}
      </>}
      {detail.section === "insights" && <>
        <p className="text-sm text-muted">{label(detail.row.kind)} · {label(detail.row.operation)} · v{detail.row.version} · {label(detail.row.status)}
          {detail.row.eligible === false && " · 证据待复核"}
        </p>
        <dl className="space-y-2 text-sm">
          {Object.entries(detail.row.body ?? {}).filter(([, value]) => value.length > 0).map(([key, value]) =>
            <div key={key}><dt className="font-medium">{label(key)}</dt><dd className="whitespace-pre-wrap text-muted">
              {key === "steps" && Array.isArray(value) ? <ol className="list-decimal pl-5">{value.map(step => <li key={step}>{step}</li>)}</ol> :
                Array.isArray(value) ? value.join("、") : value}</dd></div>)}
        </dl>
        <section>
          <h4 className="font-medium">证据 · {detail.row.support_count} 支持 · {detail.row.refute_count} 反例</h4>
          {evidenceList(detail.row.evidence ?? [])}
        </section>
        <div className="flex gap-2">
          {detail.row.status === "pending" && <>
            <button className={button} disabled={busy || detail.row.eligible === false} onClick={() => void mutate("accept")}>接受并启用</button>
            <button className={button} disabled={busy} onClick={() => void mutate("reject")}>拒绝</button>
          </>}
          {detail.row.status === "active" && <button className={button} disabled={busy} onClick={() => void mutate("disable")}>停用</button>}
          {detail.row.status === "disabled" && <button className={button} disabled={busy || detail.row.eligible === false} onClick={() => void mutate("enable")}>启用</button>}
        </div>
        {detail.row.usage && <p className="text-sm text-muted">实际使用 {detail.row.usage.samples} 次 · 正向 {detail.row.usage.helpful} · 负向 {detail.row.usage.harmful}</p>}
        {detail.row.effect && <Effects effect={detail.row.effect} />}
      </>}
    </article> : home && <div className="grid gap-2">
      {home[section].length === 0 && <p className={`${card} text-sm text-muted`}>暂无{label(section)}。</p>}
      {section === "episodes" && home.episodes.map(row => <button key={row.id} type="button" className={`${card} text-left hover:bg-surface-muted`} onClick={() => void open(section, row.id)}>
        <p className="line-clamp-2 break-words font-medium">{row.title || label(row.kind)}</p>
        <p className="mt-1 text-xs text-muted">{label(row.kind)} · {label(row.family)} · {label(row.outcome)} · {time(row.completed_at)} · {row.uses.length} 项经验</p>
      </button>)}
      {section === "episodes" && home.episodes_next_cursor &&
        <button type="button" className={button} disabled={busy || listBusy} onClick={() => void loadOlder()}>
          {listBusy ? "加载中…" : "加载更早的经历"}
        </button>}
      {section === "candidates" && home.candidates.map(row => <button key={row.candidate_id} type="button" className={`${card} text-left hover:bg-surface-muted`} onClick={() => void open(section, row.candidate_id)}>
        <p className="font-medium">{row.title}</p>
        <p className="mt-1 text-xs text-muted">{label(row.route)} · {label(row.status)}{row.progress.status && ` · ${label(row.progress.status)}`}</p>
      </button>)}
      {section === "insights" && home.insights.map(row => <button key={row.id} type="button" className={`${card} text-left hover:bg-surface-muted`} onClick={() => void open(section, row.id)}>
        <p className="font-medium">{row.title}</p>
        <p className="mt-1 text-xs text-muted">{label(row.kind)} · {label(row.family)} · v{row.version} · {label(row.status)}</p>
      </button>)}
    </div>}
  </div>;
}

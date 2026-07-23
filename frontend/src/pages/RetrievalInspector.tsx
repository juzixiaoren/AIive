/**
 * 检索检查器：按 trace_id 展示真实统一检索与记忆召回运行。
 */
import { useEffect, useState } from "react";

type RunSummary = {
  run_id: string;
  query?: string;
  strategy?: string;
};

type Candidate = {
  source_id: string;
  source_type: string;
  score: number | null;
};

type RecallCandidate = {
  memory_id: string;
  route: string;
  raw_score: number;
  fused_score: number;
  selected: boolean;
  exclusion_reason: string;
  token_cost: number;
};

type RetrievalDetail = RunSummary & { candidates: Candidate[] };
type RecallDetail = {
  run_id: string;
  request_query: string;
  routes_executed: string[];
  token_budget: number;
  result_count: number;
  total_latency_ms: number;
  candidates: RecallCandidate[];
};

export default function RetrievalInspector({ traceId }: { traceId?: string }) {
  const [retrieval, setRetrieval] = useState<RetrievalDetail | null>(null);
  const [recall, setRecall] = useState<RecallDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!traceId) {
      setRetrieval(null);
      setRecall(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([
      fetch(`/api/retrieval-runs?trace_id=${encodeURIComponent(traceId)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<RunSummary[]>;
      }),
      fetch(`/api/memory-recall-runs?trace_id=${encodeURIComponent(traceId)}`).then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<Array<{ run_id: string }>>;
      }),
    ]).then(async ([runs, recallRuns]) => {
      const [retrievalDetail, recallDetail] = await Promise.all([
        runs[0]
          ? fetch(`/api/retrieval-runs/${runs[0].run_id}`).then(r => {
              if (!r.ok) throw new Error(`HTTP ${r.status}`);
              return r.json() as Promise<RetrievalDetail>;
            })
          : Promise.resolve(null),
        recallRuns[0]
          ? fetch(`/api/memory-recall-runs/${recallRuns[0].run_id}`).then(r => {
              if (!r.ok) throw new Error(`HTTP ${r.status}`);
              return r.json() as Promise<RecallDetail>;
            })
          : Promise.resolve(null),
      ]);
      if (!cancelled) {
        setRetrieval(retrievalDetail);
        setRecall(recallDetail);
      }
    }).catch(err => {
      if (!cancelled) setError(`加载检索记录失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [traceId]);

  if (!traceId) return <div className="text-center text-faint py-16">在对话中点击 trace_id 查看检索过程</div>;
  if (loading) return <div className="text-center text-faint py-16">正在加载检索记录…</div>;
  if (error) return <div className="text-center text-danger py-16">{error}</div>;
  if (!retrieval && !recall) return <div className="text-center text-faint py-16">本轮没有检索记录</div>;

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-content">检索检查器</h2>
        <p className="text-xs text-faint font-mono break-all">trace_id: {traceId}</p>
      </div>
      {retrieval && (
        <section className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
          <div className="flex justify-between gap-3 mb-3">
            <div>
              <h3 className="text-sm font-medium text-title">统一检索</h3>
              <p className="text-xs text-faint mt-1">{retrieval.query}</p>
            </div>
            <span className="text-[11px] font-mono text-code">{retrieval.strategy}</span>
          </div>
          <div className="space-y-2">
            {retrieval.candidates.map(candidate => (
              <div key={`${candidate.source_type}:${candidate.source_id}`} className="border border-divider rounded-lg p-3">
                <div className="flex justify-between gap-3 text-xs">
                  <span className="text-content">{candidate.source_type}</span>
                  <span className="font-mono text-primary">{candidate.score?.toFixed(4) ?? "-"}</span>
                </div>
                <p className="text-[11px] text-faint font-mono break-all mt-1">{candidate.source_id}</p>
              </div>
            ))}
          </div>
        </section>
      )}
      {recall && (
        <section className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
          <div className="grid grid-cols-3 gap-3 mb-4 text-center">
            <div><p className="text-xs text-faint">命中</p><p className="font-mono text-title">{recall.result_count}</p></div>
            <div><p className="text-xs text-faint">耗时</p><p className="font-mono text-title">{recall.total_latency_ms}ms</p></div>
            <div><p className="text-xs text-faint">预算</p><p className="font-mono text-title">{recall.token_budget}</p></div>
          </div>
          <p className="text-xs text-faint mb-3">路由：{recall.routes_executed.join("、") || "无命中"}</p>
          <div className="space-y-2">
            {recall.candidates.map(candidate => (
              <div key={`${candidate.route}:${candidate.memory_id}`} className="border border-divider rounded-lg p-3">
                <div className="flex justify-between gap-3 text-xs">
                  <span className="text-content">{candidate.route}</span>
                  <span className="font-mono text-primary">{candidate.fused_score.toFixed(4)}</span>
                </div>
                <p className="text-[11px] text-faint font-mono break-all mt-1">{candidate.memory_id}</p>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

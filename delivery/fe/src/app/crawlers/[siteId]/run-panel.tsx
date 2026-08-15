"use client";

import { useState } from "react";
import { parseCrawlLimit } from "@/lib/crawl-limit";

export default function RunPanel({ siteId }: Readonly<{ siteId: string }>) {
  const [mode, setMode] = useState<"incremental" | "full">("incremental");
  const [limit, setLimit] = useState("3");
  const [fullConfirmed, setFullConfirmed] = useState(false);
  const [state, setState] = useState<{ readonly kind: "idle" | "submitting" | "success" | "error"; readonly message?: string }>({ kind: "idle" });

  const limitResult = parseCrawlLimit(limit);
  const needsFullConfirm = mode === "full" && !fullConfirmed;
  const canSubmit = state.kind !== "submitting" && limitResult.ok && !needsFullConfirm;

  const changeMode = (next: "incremental" | "full") => {
    setMode(next);
    setFullConfirmed(false);
  };

  const submit = async () => {
    if (!limitResult.ok || needsFullConfirm) return;
    setState({ kind: "submitting" });
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ siteId, mode, limit: limitResult.limit }),
      });
      const result = await response.json() as { readonly error?: string; readonly jobId?: number | string };
      if (!response.ok) throw new Error(result.error ?? "작업 등록에 실패했습니다.");
      setState({ kind: "success", message: `작업 #${String(result.jobId)}이 대기열에 등록되었습니다.` });
    } catch (error) {
      setState({ kind: "error", message: error instanceof Error ? error.message : "작업 등록에 실패했습니다." });
    }
  };

  return (
    <section className="run-panel" aria-labelledby="run-heading">
      <div className="run-heading"><div><p className="eyebrow">RUN CRAWLER</p><h2 id="run-heading">수집 실행</h2></div><span>Worker 작업 대기열에 등록</span></div>
      <div className="run-presets">
        <button className={limit === "3" ? "selected" : ""} onClick={() => setLimit("3")} type="button"><strong>상태 확인</strong><small>최대 3건</small></button>
        <button className={limit === "20" ? "selected" : ""} onClick={() => setLimit("20")} type="button"><strong>시험 수집</strong><small>최대 20건</small></button>
        <button className={limit === "100" ? "selected" : ""} onClick={() => setLimit("100")} type="button"><strong>제한 수집</strong><small>최대 100건</small></button>
      </div>
      <div className="run-fields">
        <label><span>수집 모드</span><select value={mode} onChange={(event) => changeMode(event.target.value === "full" ? "full" : "incremental")}><option value="incremental">증분 수집</option><option value="full">전체 수집</option></select></label>
        <label><span>최대 저장 건수</span><input min="1" onChange={(event) => setLimit(event.target.value)} type="number" value={limit} /></label>
        <button className="run-submit" disabled={!canSubmit} onClick={submit} type="button">{state.kind === "submitting" ? "등록 중…" : "크롤러 실행"}</button>
      </div>
      {!limitResult.ok && <p className="run-message run-message--error" role="alert">{limitResult.error}</p>}
      {mode === "full" && (
        <label className="run-confirm">
          <input checked={fullConfirmed} onChange={(event) => setFullConfirmed(event.target.checked)} type="checkbox" />
          <span>전체 수집은 대상 사이트에 많은 요청을 보냅니다. 실행하려면 확인이 필요합니다.</span>
        </label>
      )}
      <p className="run-caution">먼저 3건 상태 확인을 권장합니다. 전체 수집은 대상 사이트에 많은 요청을 보낼 수 있습니다.</p>
      {state.kind === "success" && <p className="run-message run-message--success" role="status">{state.message}</p>}
      {state.kind === "error" && <p className="run-message run-message--error" role="alert">{state.message}</p>}
    </section>
  );
}

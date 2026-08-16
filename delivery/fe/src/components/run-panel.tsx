"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { parseCrawlLimit, UNBOUNDED_CRAWL_LIMIT } from "@/lib/crawl-limit";
import { describeBlockedCrawlerWarning, type HealthState } from "@/lib/blocked-crawler-warning";
import { JOB_POLL_INTERVAL_MS, describeJobOutcome, describeTrackingFailure, isTerminalJobStatus, jobStatusLabel } from "@/lib/job-status";

type JobLog = { readonly level: string; readonly event: string; readonly message: string; readonly created_at: string };
type JobDetail = {
  readonly id: number;
  readonly site_id: string;
  readonly status: string;
  readonly saved_count: number | null;
  readonly error?: string | null;
  readonly logs?: readonly JobLog[];
};

type SubmitState =
  | { readonly kind: "idle" }
  | { readonly kind: "submitting" }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "submitted"; readonly jobId: number };

export default function RunPanel({ siteId, status, category, reason }: Readonly<{
  siteId: string;
  status: HealthState;
  category: string;
  reason: string;
}>) {
  const [mode, setMode] = useState<"incremental" | "full">("incremental");
  const [limit, setLimit] = useState("3");
  const [unbounded, setUnbounded] = useState(false);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [state, setState] = useState<SubmitState>({ kind: "idle" });

  // Tracking state is decoupled from `state` above so that a job update
  // arriving every JOB_POLL_INTERVAL_MS does not itself retrigger the
  // polling effect below (which only depends on trackingJobId/retryTick).
  const [trackingJobId, setTrackingJobId] = useState<number | null>(null);
  const [job, setJob] = useState<JobDetail | null>(null);
  const [trackingError, setTrackingError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  const limitInput: unknown = unbounded ? UNBOUNDED_CRAWL_LIMIT : limit;
  const limitResult = parseCrawlLimit(limitInput);
  const needsRiskConfirm = (mode === "full" || unbounded) && !riskConfirmed;
  const canSubmit = state.kind !== "submitting" && limitResult.ok && !needsRiskConfirm;
  const blockedWarning = describeBlockedCrawlerWarning({ status, category, reason });

  const changeMode = (next: "incremental" | "full") => {
    setMode(next);
    setRiskConfirmed(false);
  };

  const selectPreset = (value: string) => {
    setLimit(value);
    setUnbounded(false);
  };

  const selectUnbounded = () => {
    setUnbounded(true);
    setRiskConfirmed(false);
  };

  const riskMessage = mode === "full" && unbounded
    ? "전체 수집과 무제한을 함께 선택했습니다. 대상 사이트의 모든 문서를 건수 제한 없이 수집합니다. 실행하려면 확인이 필요합니다."
    : unbounded
      ? "무제한을 선택했습니다. 최대 저장 건수를 적용하지 않고 대상 사이트의 문서를 모두 수집합니다. 실행하려면 확인이 필요합니다."
      : "전체 수집은 대상 사이트에 많은 요청을 보냅니다. 실행하려면 확인이 필요합니다.";

  // Polls a submitted job until it reaches a terminal state, the job
  // disappears, or the endpoint fails. Stops scheduling further polls on
  // any of those outcomes (never spins forever on a broken connection) and
  // is cleaned up on unmount or when a new job starts tracking.
  useEffect(() => {
    if (trackingJobId === null) return undefined;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const response = await fetch(`/api/jobs/${trackingJobId}`, { cache: "no-store" });
        if (cancelled) return;
        if (!response.ok) {
          setTrackingError(describeTrackingFailure(response.status));
          return;
        }
        const payload = await response.json() as JobDetail;
        if (cancelled) return;
        setJob(payload);
        setTrackingError(null);
        if (!isTerminalJobStatus(payload.status)) {
          timer = setTimeout(() => void poll(), JOB_POLL_INTERVAL_MS);
        }
      } catch {
        if (!cancelled) setTrackingError(describeTrackingFailure(null));
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [trackingJobId, retryTick]);

  const submit = async () => {
    if (!limitResult.ok || needsRiskConfirm) return;
    setState({ kind: "submitting" });
    setJob(null);
    setTrackingError(null);
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ siteId, mode, limit: limitInput }),
      });
      const result = await response.json() as { readonly error?: string; readonly jobId?: number | string };
      if (!response.ok) throw new Error(result.error ?? "작업 등록에 실패했습니다.");
      const jobId = Number(result.jobId);
      setState({ kind: "submitted", jobId });
      setTrackingJobId(jobId);
    } catch (error) {
      setState({ kind: "error", message: error instanceof Error ? error.message : "작업 등록에 실패했습니다." });
    }
  };

  const trackedStatus = job?.status ?? null;
  const isTracking = trackingJobId !== null && !trackingError && (trackedStatus === null || !isTerminalJobStatus(trackedStatus));

  return (
    <section className="run-panel" aria-labelledby="run-heading">
      <div className="run-heading"><div><p className="eyebrow">RUN CRAWLER</p><h2 id="run-heading">수집 실행</h2></div><span>Worker 작업 대기열에 등록</span></div>
      {blockedWarning && <aside className="run-warning"><strong>실패 가능성</strong><span>{blockedWarning}</span></aside>}
      <div className="run-presets">
        <button className={!unbounded && limit === "3" ? "selected" : ""} onClick={() => selectPreset("3")} type="button"><strong>상태 확인</strong><small>최대 3건</small></button>
        <button className={!unbounded && limit === "20" ? "selected" : ""} onClick={() => selectPreset("20")} type="button"><strong>시험 수집</strong><small>최대 20건</small></button>
        <button className={!unbounded && limit === "100" ? "selected" : ""} onClick={() => selectPreset("100")} type="button"><strong>제한 수집</strong><small>최대 100건</small></button>
        <button className={unbounded ? "preset-unbounded selected" : "preset-unbounded"} onClick={selectUnbounded} type="button"><strong>무제한 수집</strong><small>건수 제한 없음</small></button>
      </div>
      <div className="run-fields">
        <label><span>수집 모드</span><select value={mode} onChange={(event) => changeMode(event.target.value === "full" ? "full" : "incremental")}><option value="incremental">증분 수집</option><option value="full">전체 수집</option></select></label>
        <label>
          <span>최대 저장 건수</span>
          <input disabled={unbounded} min="1" onChange={(event) => setLimit(event.target.value)} type="number" value={unbounded ? "" : limit} placeholder={unbounded ? "무제한 선택됨" : undefined} />
        </label>
        <button className="run-submit" disabled={!canSubmit} onClick={submit} type="button">{state.kind === "submitting" ? "등록 중…" : "크롤러 실행"}</button>
      </div>
      {unbounded && <p className="run-note">무제한을 선택하면 최대 저장 건수 입력은 적용되지 않습니다.</p>}
      {!limitResult.ok && <p className="run-message run-message--error" role="alert">{limitResult.error}</p>}
      {(mode === "full" || unbounded) && (
        <label className="run-confirm">
          <input checked={riskConfirmed} onChange={(event) => setRiskConfirmed(event.target.checked)} type="checkbox" />
          <span>{riskMessage}</span>
        </label>
      )}
      <p className="run-caution">먼저 3건 상태 확인을 권장합니다. 전체 수집이나 무제한 수집은 대상 사이트의 모든 문서를 대상으로 많은 요청을 보낼 수 있습니다.</p>
      {state.kind === "error" && <p className="run-message run-message--error" role="alert">{state.message}</p>}
      {state.kind === "submitted" && (
        <div className="run-tracking" role="status">
          <p className="run-message run-message--success">작업 #{state.jobId}이 대기열에 등록되었습니다.</p>
          <div className={`run-tracking-status${job ? ` run-tracking-status--${job.status}` : ""}`}>
            <span>{job ? jobStatusLabel(job.status) : "상태 확인 중…"}</span>
            {isTracking && <span className="run-tracking-live">자동으로 상태를 확인하는 중</span>}
          </div>
          {trackingError && (
            <p className="run-message run-message--error" role="alert">
              {trackingError}{" "}
              <button className="run-tracking-retry" onClick={() => setRetryTick((tick) => tick + 1)} type="button">다시 확인</button>
            </p>
          )}
          {job && isTerminalJobStatus(job.status) && (
            <p className="run-tracking-outcome">{describeJobOutcome(job)}</p>
          )}
          {job && job.logs && job.logs.length > 0 && (
            <div className="run-tracking-logs">
              {job.logs.map((log, index) => (
                <p key={`${log.created_at}-${index}`}>
                  <time>{new Date(log.created_at).toLocaleTimeString("ko-KR")}</time>
                  <span>{log.message}</span>
                </p>
              ))}
            </div>
          )}
          <p className="run-tracking-link"><Link href="/#jobs">작업 현황에서 전체 목록 보기 →</Link></p>
        </div>
      )}
    </section>
  );
}

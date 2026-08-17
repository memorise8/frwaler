"use client";

import { useState } from "react";
import {
  BULK_LIMIT_MAX,
  BULK_LIMIT_MIN,
  DEFAULT_BULK_LIMIT,
  describeBulkCancel,
  describeBulkRun,
  MAX_BULK_SITES,
  parseBulkRunRequest,
  type BulkOutcome,
  type BulkSummary,
  type CancelSummary,
} from "@/lib/bulk-run";

type State =
  | { kind: "idle" }
  | { kind: "working"; verb: string }
  | { kind: "error"; message: string }
  | { kind: "ran"; summary: BulkSummary; problems: readonly BulkOutcome[] }
  | { kind: "cancelled"; summary: CancelSummary };

type CancelScope = Readonly<{ scope: "jobs"; jobIds: readonly number[] } | { scope: "all" }>;

export default function BulkRunPanel({ siteIds, label }: Readonly<{ siteIds: readonly string[]; label: string }>) {
  const [mode, setMode] = useState<"incremental" | "full">("incremental");
  const [limit, setLimit] = useState(String(DEFAULT_BULK_LIMIT));
  const [confirmed, setConfirmed] = useState(false);
  const [state, setState] = useState<State>({ kind: "idle" });
  // Ids this panel created, so its stop button can name its own work. Lost on
  // reload, which is exactly when the whole-queue scope earns its place.
  const [startedJobIds, setStartedJobIds] = useState<readonly number[]>([]);
  const [confirmStopAll, setConfirmStopAll] = useState(false);

  const parsed = parseBulkRunRequest({ siteIds, mode, limit: Number(limit) });
  const working = state.kind === "working" ? state.verb : null;
  const busy = working !== null;
  const canRun = parsed.ok && confirmed && !busy;

  const run = async () => {
    if (!parsed.ok || !confirmed) return;
    setState({ kind: "working", verb: "등록" });
    try {
      const response = await fetch("/api/jobs/bulk", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ siteIds, mode, limit: Number(limit) }),
      });
      const payload = await response.json() as { error?: string; summary?: BulkSummary; problems?: readonly BulkOutcome[]; jobIds?: readonly number[] };
      if (!response.ok || !payload.summary) {
        setState({ kind: "error", message: payload.error ?? "일괄 실행에 실패했습니다." });
        return;
      }
      setState({ kind: "ran", summary: payload.summary, problems: payload.problems ?? [] });
      setStartedJobIds(payload.jobIds ?? []);
      setConfirmed(false);
    } catch {
      setState({ kind: "error", message: "작업 등록 요청을 보내지 못했습니다. 연결을 확인해 주세요." });
    }
  };

  const cancel = async (target: CancelScope) => {
    setState({ kind: "working", verb: "취소" });
    try {
      const response = await fetch("/api/jobs/bulk/cancel", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(target),
      });
      const payload = await response.json() as { error?: string; summary?: CancelSummary };
      if (!response.ok || !payload.summary) {
        setState({ kind: "error", message: payload.error ?? "일괄 중지에 실패했습니다." });
        return;
      }
      setState({ kind: "cancelled", summary: payload.summary });
      if (target.scope === "jobs") setStartedJobIds([]);
      setConfirmStopAll(false);
    } catch {
      setState({ kind: "error", message: "중지 요청을 보내지 못했습니다. 연결을 확인해 주세요." });
    }
  };

  const count = siteIds.length;
  const perSite = Number(limit);
  const worstCase = Number.isFinite(perSite) ? count * perSite : 0;

  return (
    <section className="run-panel bulk-run" aria-labelledby="bulk-heading">
      <div className="run-heading">
        <div><p className="eyebrow">BULK RUN</p><h2 id="bulk-heading">목록 전체 수집</h2></div>
        <span>{label} · {count.toLocaleString("ko-KR")}개 사이트</span>
      </div>

      {count > MAX_BULK_SITES ? (
        <p className="run-message run-message--error" role="alert">
          한 번에 실행할 수 있는 사이트는 최대 {MAX_BULK_SITES.toLocaleString("ko-KR")}개입니다. 조건을 좁혀 주세요.
        </p>
      ) : (
        <>
          <div className="run-fields">
            <label>
              <span>수집 모드</span>
              <select value={mode} onChange={(event) => { setMode(event.target.value === "full" ? "full" : "incremental"); setConfirmed(false); }} disabled={busy}>
                <option value="incremental">증분 수집</option>
                <option value="full">전체 수집</option>
              </select>
            </label>
            <label>
              <span>사이트당 최대 건수</span>
              <input
                type="number"
                min={BULK_LIMIT_MIN}
                max={BULK_LIMIT_MAX}
                value={limit}
                onChange={(event) => { setLimit(event.target.value); setConfirmed(false); }}
                disabled={busy}
              />
            </label>
            <button className="run-submit" type="button" onClick={() => void run()} disabled={!canRun}>
              {working === "등록" ? "등록 중…" : `${count.toLocaleString("ko-KR")}개 모두 수집`}
            </button>
          </div>

          {!parsed.ok && <p className="run-message run-message--error" role="alert">{parsed.error}</p>}

          <label className="run-confirm">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} disabled={busy} />
            <span>
              {count.toLocaleString("ko-KR")}개 사이트에 각각 최대 {Number.isFinite(perSite) ? perSite.toLocaleString("ko-KR") : "?"}건
              {mode === "full" ? ", 전체 수집 모드" : ""}으로 작업을 등록합니다. 최대 {worstCase.toLocaleString("ko-KR")}건을 외부 사이트에서 내려받게 되며,
              워커가 한 번에 한 사이트씩 처리하므로 모두 끝나기까지 수 시간이 걸릴 수 있습니다. 실행하려면 확인이 필요합니다.
            </span>
          </label>

          <p className="run-caution">
            등록은 즉시 끝나고, 수집은 워커가 이어서 진행합니다. 이 화면을 닫아도 계속됩니다.
            이미 실행 중인 사이트는 자동으로 건너뜁니다. 멈추려면 아래 중지를 누르세요.
          </p>
        </>
      )}

      <div className="bulk-actions">
        {startedJobIds.length > 0 && (
          <button
            type="button"
            onClick={() => void cancel({ scope: "jobs", jobIds: startedJobIds })}
            disabled={busy}
          >
            {working === "취소" ? "중지 중…" : `방금 등록한 ${startedJobIds.length.toLocaleString("ko-KR")}건 중지`}
          </button>
        )}
        <button
          type="button"
          className="bulk-stop-all"
          onClick={() => void cancel({ scope: "all" })}
          disabled={busy || !confirmStopAll}
        >
          대기열 전체 중지
        </button>
        <label className="bulk-stop-confirm">
          <input type="checkbox" checked={confirmStopAll} onChange={(event) => setConfirmStopAll(event.target.checked)} disabled={busy} />
          <span>
            대기열 전체 중지는 이 화면에서 등록한 작업만이 아니라 <strong>예약이 만든 작업과 다른 사람이 시작한 작업까지</strong> 함께 취소합니다.
            {startedJobIds.length > 0 ? " 방금 등록한 것만 멈추려면 왼쪽 버튼을 쓰세요." : " 방금 등록한 작업 번호를 알 수 없을 때만 사용하세요."}
          </span>
        </label>
      </div>

      {state.kind === "error" && <p className="run-message run-message--error" role="alert">{state.message}</p>}

      {state.kind === "ran" && (
        <div className="run-tracking" role="status">
          <p className="run-message run-message--success">{describeBulkRun(state.summary)}</p>
          {state.problems.length > 0 && (
            <div className="run-tracking-logs">
              {state.problems.map((problem) => (
                <p key={problem.siteId}>
                  <time>{problem.kind === "skipped" ? "건너뜀" : "실패"}</time>
                  <span><code>{problem.siteId}</code> {problem.kind === "queued" ? "" : problem.reason}</span>
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      {state.kind === "cancelled" && (
        <p className="run-message run-message--success" role="status">{describeBulkCancel(state.summary)}</p>
      )}
    </section>
  );
}

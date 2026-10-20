"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { crawlerDocumentsHref } from "@/lib/crawler-evidence";
import { formatHealthDate as dateText } from "@/lib/health-status";
import {
  CLASS_LABELS, classLabel, countByClass, filterSites, isRunActive, laneOptions, needsApproval, performAction,
  progressPercent, runModeLabel, runStatusLabel, stateLabel, TRIAL_LIMIT_MAX, UNCLASSIFIED,
  type CollectAction, type CollectSite, type CollectSitesReport, type RunnerHealth, type UncollectedRow,
} from "@/lib/site-collect";

const PAGE_SIZE = 40;
const CLASS_ORDER = ["run", "dev", "nego", "skip", UNCLASSIFIED] as const;
const classTone = (value: string): string => value === "run" ? "healthy" : value === "nego" ? "unhealthy" : "unknown";

type Message = Readonly<{ ok: boolean; text: string }>;

const count = (value: number | null): string => value === null ? "—" : value.toLocaleString("ko-KR");

export default function SiteCollectConsole({ report, uncollected, runner }: Readonly<{
  report: CollectSitesReport | null; uncollected: readonly UncollectedRow[]; runner: RunnerHealth | null;
}>) {
  const router = useRouter();
  const [view, setView] = useState<"all" | "approval">("all");
  const [cls, setCls] = useState("");
  const [lane, setLane] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [trialLimit, setTrialLimit] = useState(100);
  const [busy, setBusy] = useState<string | null>(null);
  const [messages, setMessages] = useState<Record<string, Message>>({});
  // 이 화면에서 요청했다가 대량 승인 필요로 거절된 사이트. BE 목록에 반영되기 전에도 승인 대기에 보인다.
  const [held, setHeld] = useState<readonly string[]>([]);

  useEffect(() => {
    const timer = window.setInterval(() => router.refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [router]);

  const sites = useMemo(() => report?.sites ?? [], [report]);
  const reasons = useMemo(() => new Map(uncollected.map((row) => [row.site_id, row])), [uncollected]);
  const approval = sites.filter((site) => needsApproval(site) || held.includes(site.site_id));
  const base = view === "approval" ? approval : sites;
  const rows = filterSites(base, { cls, lane, q: query }, reasons);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const currentPage = Math.min(page, pages);
  const visible = rows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const classCounts = countByClass(sites);
  const lanes = laneOptions(sites);
  const docTotal = sites.reduce((sum, site) => sum + (site.doc_count ?? 0), 0);
  const expectedTotal = sites.reduce((sum, site) => sum + (site.expected ?? 0), 0);
  const activeRuns = sites.filter((site) => isRunActive(site.latest_run)).length;
  const runnerDown = report !== null && !report.runner_ok;
  const runnerPaused = runner?.paused === true;

  const act = async (action: CollectAction) => {
    setBusy(action.siteId);
    const outcome = await performAction(action, { confirm: (text) => window.confirm(text), fetch: (...args) => fetch(...args) });
    setBusy(null);
    if (outcome.kind === "skipped") return;
    setMessages((current) => ({ ...current, [action.siteId]: { ok: outcome.kind === "ok", text: outcome.message } }));
    if (outcome.kind === "rejected" && outcome.reason === "large_needs_approval") {
      setHeld((current) => current.includes(action.siteId) ? current : [...current, action.siteId]);
    }
    if (outcome.kind === "ok") router.refresh();
  };

  const resetPage = () => setPage(1);

  return <div className="status-page health-page site-collect-page">
    <div className="section-heading">
      <div><h1>사이트 수집</h1><p>사이트별 판정·미수집 사유·최근 실행을 한 표에서 보고, 시험 수집이나 이어받기를 실행기에 요청합니다. 30초마다 표시를 갱신합니다. 시각은 한국 시간입니다.</p></div>
      <button type="button" onClick={() => router.refresh()}>새로고침</button>
    </div>
    <aside className="snapshot-note">
      <strong>실행 기준</strong><span>시작 가능 여부는 실행기가 판단합니다. 거절되면 사유를 각 사이트 줄에 표시합니다.
        접근 제한(robots 거부, 로그인, 챌린지)은 이 화면에서 해제하거나 우회할 수 없습니다. 요청 시각이 실행기에 기록됩니다.</span>
    </aside>

    {!report ? <p role="alert" className="run-message run-message--error">사이트 수집 현황을 불러올 수 없습니다. 백엔드 연결·인증 또는 배포 버전을 확인하세요.</p> : <>
      {runnerDown && <p role="alert" className="run-message run-message--error">실행기에 연결할 수 없습니다{runner?.detail ? ` (${runner.detail})` : ""}. 표는 볼 수 있지만 수집 요청은 막아 둡니다.</p>}
      {runnerPaused && <p role="alert" className="run-message run-message--error">실행기가 일시 중지돼 있습니다. 새 작업 요청은 거절됩니다.</p>}
      {!report.status_dir_ok && <p role="alert" className="run-message run-message--error">판정 파일(site-status) 폴더를 읽지 못했습니다. 판정·구분 값이 비어 있거나 오래됐을 수 있습니다.</p>}
      <div className="summary-grid">
        <article className="summary-card"><span>사이트</span><strong>{sites.length.toLocaleString("ko-KR")}</strong><small>실행 가능 {classCounts.run ?? 0} · 개발 필요 {classCounts.dev ?? 0} · 협의 필요 {classCounts.nego ?? 0}</small></article>
        <article className="summary-card"><span>수집 문서 / 예상</span><strong>{docTotal.toLocaleString("ko-KR")}</strong><small>{expectedTotal ? `예상 총건수가 확인된 사이트 합계 ${expectedTotal.toLocaleString("ko-KR")}` : "예상 총건수 미확인"}</small></article>
        <article className="summary-card"><span>대기·실행 중</span><strong>{activeRuns}</strong><small>승인 대기 {approval.length}곳</small></article>
      </div>
      <div className="collect-tabs" role="tablist" aria-label="보기">
        <button type="button" role="tab" aria-selected={view === "all"} className={view === "all" ? "collect-tab collect-tab--active" : "collect-tab"} onClick={() => {setView("all"); resetPage();}}>전체 사이트</button>
        <button type="button" role="tab" aria-selected={view === "approval"} className={view === "approval" ? "collect-tab collect-tab--active" : "collect-tab"} onClick={() => {setView("approval"); resetPage();}}>승인 대기 ({approval.length})</button>
      </div>
      {view === "approval" && <p className="run-note">이어받기(전체 수집) 시 대량 승인이 필요한 사이트입니다(예상 건수 10000건 이상 또는 미확인). 시험 수집은 승인 없이 요청할 수 있습니다. 승인은 실행기 운영 책임자가 처리하며, 승인 뒤 이어받기를 다시 요청하세요.</p>}
      <div className="filters filters--collect">
        <label><span>사이트 검색</span><input value={query} onChange={(e) => {setQuery(e.target.value); resetPage();}} placeholder="사이트 ID, 사유, 구역" /></label>
        <label><span>구분</span><select value={cls} onChange={(e) => {setCls(e.target.value); resetPage();}}>
          <option value="">전체 구분</option>{CLASS_ORDER.map((key) => <option value={key} key={key}>{CLASS_LABELS[key === UNCLASSIFIED ? "" : key]} ({classCounts[key] ?? 0})</option>)}
        </select></label>
        <label><span>구역</span><select value={lane} disabled={!lanes.length} onChange={(e) => {setLane(e.target.value); resetPage();}}>
          <option value="">{lanes.length ? "전체 구역" : "구역 정보 없음"}</option>{lanes.map((name) => <option value={name} key={name}>{name}</option>)}
        </select></label>
        <label><span>시험 수집 건수</span><input type="number" min={1} max={TRIAL_LIMIT_MAX} value={trialLimit}
          onChange={(e) => { const value = Number.parseInt(e.target.value, 10); if (value > 0) setTrialLimit(Math.min(TRIAL_LIMIT_MAX, value)); }} /></label>
      </div>
      <p className="run-note">{rows.length.toLocaleString("ko-KR")}곳 표시. 사이트 이름을 누르면 크롤러 상세, &ldquo;문서&rdquo;를 누르면 그 사이트 문서만 문서 탐색에 표시됩니다.</p>
      <div className="table-shell"><table>
        <thead><tr><th>사이트</th><th>판정</th><th>미수집 사유</th><th>문서 / 예상</th><th>최근 실행</th><th>작업</th></tr></thead>
        <tbody>{visible.map((site) => <SiteRow key={site.site_id} site={site} reason={reasons.get(site.site_id)} message={messages[site.site_id]}
          busy={busy === site.site_id} disabled={busy !== null || runnerDown} trialLimit={trialLimit} onAction={act} />)}</tbody>
      </table></div>
      {!visible.length && <p>{view === "approval" ? "승인 대기 중인 사이트가 없습니다." : "조건에 맞는 사이트가 없습니다."}</p>}
      <div className="section-heading"><button type="button" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>이전</button>
        <span>{currentPage} / {pages}</span><button type="button" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>다음</button></div>
    </>}
  </div>;
}

export function SiteRow({ site, reason, message, busy, disabled, trialLimit, onAction }: Readonly<{
  site: CollectSite; reason: UncollectedRow | undefined; message: Message | undefined; busy: boolean; disabled: boolean;
  trialLimit: number; onAction: (action: CollectAction) => void;
}>) {
  const run = site.latest_run;
  const active = isRunActive(run);
  const docPct = progressPercent(site.doc_count, site.expected);
  const runPct = run ? progressPercent(run.done, run.total) : null;
  const blocked = site.class === "nego" || site.class === "skip";
  return <tr>
    <td><Link href={`/crawlers/${encodeURIComponent(site.site_id)}`}>{site.site_id}</Link>
      <small className="reason">{[site.lane && `구역 ${site.lane}`, site.file].filter(Boolean).join(" · ") || "구역 미지정"}</small></td>
    <td><span className={`health-badge health-badge--${classTone(site.class)}`}><i />{classLabel(site.class)}</span>
      <small className="reason">{stateLabel(site.state)}{site.updated_at ? ` · ${dateText(site.updated_at)}` : ""}</small>
      <small className="reason">{site.plan === null ? "실행기 plan 없음" : site.plan.ready ? "plan 실행 가능" : `plan 준비 안 됨${site.plan.why ? ` · ${site.plan.why}` : ""}`}</small></td>
    <td>{reason?.reason || site.detail || "—"}
      {reason?.note && <small className="reason">{reason.note}</small>}
      {reason?.reason && site.detail && reason.reason !== site.detail && <small className="reason">{site.detail}</small>}
      {site.reason_codes.length > 0 && <small className="reason"><code>{site.reason_codes.join(", ")}</code></small>}</td>
    <td className="number">{count(site.doc_count)} / {site.expected === null ? "미확인" : count(site.expected)}
      {docPct !== null && <span className="collect-bar" aria-label={`수집률 ${docPct.toFixed(1)}%`}><b style={{ width: `${docPct}%` }} /></span>}
      {docPct !== null && <small className="reason">{docPct < 1 ? docPct.toFixed(2) : docPct.toFixed(1)}%</small>}</td>
    <td>{run ? <>
      {runStatusLabel(run.status)}{run.mode && ` · ${runModeLabel(run.mode)}`}
      <small className="reason">{run.run_id} · 시작 {dateText(run.started)}{run.finished ? ` · 종료 ${dateText(run.finished)}` : ""}</small>
      {(run.done !== null || run.total !== null) && <small className="reason">진행 {count(run.done)}{run.total !== null ? ` / ${count(run.total)}` : ""}{runPct !== null ? ` (${runPct.toFixed(1)}%)` : ""}</small>}
      {run.result && <small className="reason">{run.result}</small>}
    </> : "실행 기록 없음"}</td>
    <td><div className="collect-actions">
      <button type="button" disabled={disabled || active || blocked} onClick={() => onAction({ kind: "trial", siteId: site.site_id, limit: trialLimit })}>시험 수집</button>
      <button type="button" disabled={disabled || active || blocked} onClick={() => onAction({ kind: "resume", siteId: site.site_id })}>{run ? "이어받기" : "수집 시작"}</button>
      {active && run && <button type="button" disabled={disabled} onClick={() => onAction({ kind: "cancel", siteId: site.site_id, runId: run.run_id })}>취소</button>}
      <Link href={crawlerDocumentsHref(site.site_id)}>문서</Link>
    </div>
    {busy && <small className="reason">요청 보내는 중…</small>}
    {blocked && <small className="reason">{site.class === "nego" ? "협의 필요 사이트는 시작할 수 없습니다" : "대상 아님"}</small>}
    {message && <p role={message.ok ? "status" : "alert"} className={`run-message ${message.ok ? "run-message--success" : "run-message--error"}`}>{message.text}</p>}</td>
  </tr>;
}

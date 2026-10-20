// 수집 콘솔(첫 화면 "/"): 예전 로컬 수집 콘솔(plan-dev-20261008/dashboard/index.html)의
// 상태·구분 판단과 필터를 운영 BE 응답(/collect/*, /stats, /progress) 위로 옮긴 것.
//
// 화면 코드는 여기 함수만 부른다. BE 응답은 모두 normalize* 를 거치고, 빠진 필드는
// null 로 둔다(모르면 비율·상태를 지어내지 않는다 — 콘솔과 같은 원칙).

import {
  type DirectProgress, needsApproval, normalizeRunnerHealth, normalizeSitesReport, normalizeUncollected, REJECTION_LABELS, stateLabel,
  type CollectPlan, type CollectSite, type UncollectedRow,
} from "./site-collect";

// ---- 상태(콘솔 STATE) -------------------------------------------------------

export type ConsoleState = "run" | "stop" | "review" | "done" | "lock" | "idle";
export const STATE_KEYS: readonly ConsoleState[] = ["run", "stop", "review", "done", "lock", "idle"];
export const STATES: Readonly<Record<ConsoleState, Readonly<{ label: string; mark: string }>>> = {
  run: { label: "수집 중", mark: "" },
  stop: { label: "멈춤", mark: "!" },
  review: { label: "검토 필요", mark: "?" },
  done: { label: "완료", mark: "✓" },
  lock: { label: "접근 제한", mark: "⊘" },
  idle: { label: "미시작", mark: "○" },
};
export const STATE_ORDER: Readonly<Record<ConsoleState, number>> = { stop: 0, review: 1, run: 2, done: 3, lock: 4, idle: 5 };
export const MIX_ORDER: readonly ConsoleState[] = ["done", "run", "review", "stop", "lock", "idle"];

// ---- 실행 구분(콘솔 CLS) ----------------------------------------------------

export type ClassKey = "run" | "dev" | "nego" | "skip" | "none";
export const CLASS_KEYS: readonly ClassKey[] = ["run", "dev", "nego", "skip", "none"];
export const CLASSES: Readonly<Record<ClassKey, Readonly<{ label: string; tone: string }>>> = {
  run: { label: "실행 가능", tone: "c-run" },
  dev: { label: "개발 필요", tone: "c-dev" },
  nego: { label: "협의 필요", tone: "c-nego" },
  skip: { label: "대상 아님", tone: "c-skip" },
  none: { label: "판정 없음", tone: "c-skip" },
};

export const RESULT_STATUS_KO: Readonly<Record<string, string>> = {
  running: "실행 중", complete: "완료", complete_with_review: "완료(검토 필요)", complete_with_mismatch: "완료(총건수 불일치)",
  incomplete: "미완료", config_error: "설정 오류", access_blocked: "접근 차단",
};
export const RUNNER_STATUS_KO: Readonly<Record<string, string>> = {
  queued: "대기 중", running: "실행 중", done: "완료", failed: "실패", cancelled: "취소됨", cancelling: "취소 중",
};
const ACTIVE = ["queued", "running", "cancelling"];
export const STALL_SECONDS = 1800;
export const UNCLASS = "미분류";

// ---- 공통 파서 ---------------------------------------------------------------

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string => (typeof value === "string" ? value : typeof value === "number" ? String(value) : "");
const strOrNull = (value: unknown): string | null => str(value) || null;
const num = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
};
const listOf = (raw: unknown, key: string): readonly unknown[] =>
  Array.isArray(raw) ? raw : isRecord(raw) && Array.isArray(raw[key]) ? raw[key] as unknown[] : [];

/** "2026-10-20 01:02:03+00", ISO, "...Z" → epoch 초. 못 읽으면 null. */
export const tsOf = (value: string | null | undefined): number | null => {
  if (!value) return null;
  const t = Date.parse(String(value).replace(" ", "T").replace(/\+00$/, "Z"));
  return Number.isNaN(t) ? null : t / 1000;
};

/** 콘솔 ago(): 초 → "n초 전 / n분 전 / n.n시간 전 / n일 전". */
export const ago = (seconds: number | null): string => {
  if (seconds === null || !Number.isFinite(seconds)) return "-";
  const s = Math.max(0, seconds);
  if (s < 90) return `${Math.round(s)}초 전`;
  if (s < 5400) return `${Math.round(s / 60)}분 전`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}시간 전`;
  return `${Math.round(s / 86400)}일 전`;
};

export const fmt = (value: number | null | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value) ? "-" : Math.round(value).toLocaleString("ko-KR");

export const pctText = (pct: number): string => (pct < 1 ? pct.toFixed(2) : pct.toFixed(1));

// ---- BE 응답 모양 -------------------------------------------------------------

/** 실행기 run_row (deploy/collect-runner/runner.py) 그대로. */
export type RunnerRun = Readonly<{
  run_id: string;
  site_id: string;
  mode: string | null;
  limit: number | null;
  status: string;
  run_folder: string | null;
  started_at: string | null;
  finished_at: string | null;
  requested_at: string | null;
  requested_by: string | null;
  reason: string | null;
  result: Readonly<{ site_status: string | null; stop_reason: string | null; saved: number | null; failed: number | null; error: string | null }> | null;
}>;

export const normalizeRunnerRun = (raw: unknown): RunnerRun | null => {
  if (!isRecord(raw) || !str(raw.run_id)) return null;
  const result = isRecord(raw.result) ? raw.result : null;
  return {
    run_id: str(raw.run_id), site_id: str(raw.site_id), mode: strOrNull(raw.mode), limit: num(raw.limit),
    status: str(raw.status) || "unknown", run_folder: strOrNull(raw.run_folder),
    started_at: strOrNull(raw.started_at), finished_at: strOrNull(raw.finished_at),
    requested_at: strOrNull(raw.requested_at), requested_by: strOrNull(raw.requested_by), reason: strOrNull(raw.reason),
    result: result ? {
      site_status: strOrNull(result.site_status), stop_reason: strOrNull(result.stop_reason),
      saved: num(result.saved), failed: num(result.failed), error: strOrNull(result.error),
    } : null,
  };
};

/** GET /collect/runs → 실행기 runs 배열(최신 먼저). 오류 본문({reason})이면 빈 목록. */
export const normalizeRunnerRuns = (raw: unknown): RunnerRun[] =>
  listOf(raw, "runs").map(normalizeRunnerRun).filter((run): run is RunnerRun => run !== null);

/** GET /progress 의 sites[] 한 줄 (delivery 백필 진행). */
export type BackfillRow = Readonly<{
  site_id: string;
  items_done: number;
  total_estimate: number | null;
  updated_at: string | null;
  completed_at: string | null;
  active_status: string | null;
  active_job_id: number | null;
  docs_in_db: number | null;
  has_cursor: boolean;
}>;

export const normalizeProgress = (raw: unknown): BackfillRow[] =>
  listOf(raw, "sites").flatMap((row) => {
    if (!isRecord(row) || !str(row.site_id)) return [];
    return [{
      site_id: str(row.site_id), items_done: num(row.items_done) ?? 0, total_estimate: num(row.total_estimate),
      updated_at: strOrNull(row.updated_at), completed_at: strOrNull(row.completed_at),
      active_status: strOrNull(row.active_status), active_job_id: num(row.active_job_id), docs_in_db: num(row.docs_in_db),
      has_cursor: isRecord(row.cursor) && Object.keys(row.cursor).length > 0,
    }];
  });

/** GET /stats 의 overview. */
export type StatsOverview = Readonly<{
  sites: number | null; documents: number | null; pdf_downloaded: number | null; text_extracted: number | null;
  latest_collected_at: string | null; measured_at: string | null;
}>;

export const normalizeStats = (raw: unknown): StatsOverview | null => {
  if (!isRecord(raw) || !isRecord(raw.overview)) return null;
  const o = raw.overview;
  return {
    sites: num(o.sites), documents: num(o.documents), pdf_downloaded: num(o.pdf_downloaded), text_extracted: num(o.text_extracted),
    latest_collected_at: strOrNull(o.latest_collected_at), measured_at: strOrNull(raw.measured_at),
  };
};

/** GET /collect/runner/health: {runner_ok, reason, detail, paused, max_parallel, running, queued}. */
export type RunnerStatus = Readonly<{
  ok: boolean; reason: string | null; detail: string | null; paused: boolean;
  max_parallel: number | null; running: number | null; queued: number | null;
}>;

export const normalizeRunnerStatus = (raw: unknown): RunnerStatus | null => {
  const base = normalizeRunnerHealth(raw);
  if (!base || !isRecord(raw)) return null;
  return {
    ok: base.ok, paused: base.paused, reason: strOrNull(raw.reason), detail: strOrNull(raw.detail),
    max_parallel: num(raw.max_parallel), running: num(raw.running), queued: num(raw.queued),
  };
};

export const runnerDownText = (runner: RunnerStatus | null, reportReason: string | null = null): string => {
  const reason = runner?.reason ?? reportReason;
  const label = reason ? REJECTION_LABELS[reason] ?? reason : "연결 안 됨";
  return runner?.detail && runner.detail !== reason ? `${label} (${runner.detail})` : label;
};

// ---- 사이트 한 줄 ---------------------------------------------------------------

export type Taxon = Readonly<{ country?: string; doc_type?: string }>;
export type Taxonomy = Readonly<{ version?: string; sites: Readonly<Record<string, Taxon>> }>;

/** label: 구분 이름을 CLASSES 와 다르게 보일 때(예: DB 에만 있는 scienceon-api 의 "API 수집"). */
export type SiteClass = Readonly<{ k: ClassKey; why: string; src: string; lane: string | null; label?: string }>;
export const classText = (cls: SiteClass): string => cls.label || CLASSES[cls.k].label;
export type Uncollected = Readonly<{ reasons: readonly string[]; note: string }>;

export type ConsoleSite = Readonly<{
  id: string;
  name: string;
  type: string;
  country: string;
  state: ConsoleState;
  alert: string | null;
  why: string;
  cls: SiteClass;
  saved: number;
  savedSrc: string | null;
  failed: number;
  total: number | null;
  totalSrc: string | null;
  lastTs: number | null;
  documents: number | null;
  plan: CollectPlan | null;
  planInfo: Readonly<{ strategy: string | null; folder: string | null }> | null;
  runs: readonly RunnerRun[];
  backfill: BackfillRow | null;
  status: Readonly<{ state: string; detail: string; codes: readonly string[]; file: string; updated_at: string | null }> | null;
  approval: boolean;
  uncollected: Uncollected | null;
}>;

const UNCOLLECTED_LABELS: Readonly<Record<string, string>> = {
  class_dev: "개발 필요", class_nego: "협의 필요", class_skip: "대상 아님", class_none: "판정 없음", no_documents: "수집 문서 0건",
};

/** 직접 실행 진행 한 줄: "수집 중(직접 실행) · 저장 N건 · 갱신 시각". */
export const directText = (direct: DirectProgress, now: number): string => {
  const updated = tsOf(direct.updated_at);
  return ["수집 중(직접 실행)", direct.saved !== null ? `저장 ${fmt(direct.saved)}건` : null,
    updated !== null ? `갱신 ${ago(now - updated)}` : null].filter(Boolean).join(" · ");
};

/** 실행기 결과·백필 진행·직접 실행으로 콘솔 상태를 정한다 (콘솔 stateOf 를 운영 데이터로). */
export const stateOf = (run: RunnerRun | null, backfill: BackfillRow | null, now: number, direct: DirectProgress | null = null):
  Readonly<{ state: ConsoleState; alert: string | null; why: string }> => {
  // 실행기 밖에서 직접 돌리는 수집: 실행기 기록이 비어 있어도 '수집 중'으로 보인다.
  if (direct?.running && !(run && ACTIVE.includes(run.status))) return { state: "run", alert: null, why: directText(direct, now) };
  if (backfill?.active_status === "running") {
    const since = tsOf(backfill.updated_at);
    if (since !== null && now - since >= STALL_SECONDS) {
      return { state: "stop", alert: `멈춤 의심 · 마지막 갱신 ${Math.round((now - since) / 60)}분 전 (백필 작업 #${backfill.active_job_id ?? "?"})`, why: "" };
    }
  }
  if ((run && ACTIVE.includes(run.status)) || (backfill?.active_status && ACTIVE.includes(backfill.active_status))) {
    const queued = run?.status === "queued" && !(backfill?.active_status === "running");
    return { state: "run", alert: null, why: queued ? "대기 중" : run?.status === "cancelling" ? "취소 중" : "" };
  }
  if (run) {
    if (run.status === "failed") return { state: "stop", alert: `멈춤: 실행기 작업 ${run.run_id} 실패${run.reason ? ` · ${run.reason}` : ""}`, why: "" };
    if (run.status === "cancelled") return { state: "review", alert: null, why: "취소됨" };
    const r = run.result;
    if (r?.error) return { state: "review", alert: null, why: "결과 읽기 실패" };
    if (r?.site_status === "access_blocked") return { state: "lock", alert: null, why: r.stop_reason === "robots_disallowed" ? "robots.txt 금지" : r.stop_reason ?? "" };
    if (r?.site_status && r.site_status !== "complete") {
      return { state: "review", alert: null, why: r.site_status !== "complete_with_review" ? RESULT_STATUS_KO[r.site_status] ?? r.site_status : "" };
    }
    return { state: "done", alert: null, why: run.mode === "trial" ? "시험 수집" : "" };
  }
  if (backfill?.completed_at) return { state: "done", alert: null, why: "백필 완주" };
  if (backfill?.has_cursor) return { state: "review", alert: null, why: "백필 중단됨" };
  return { state: "idle", alert: null, why: "" };
};

/** 콘솔 classOf: 실행 기록(최우선) > site-status 판정 파일. */
export const classOf = (site: CollectSite | null, run: RunnerRun | null, state: ConsoleState, why: string): SiteClass => {
  const lane = site?.lane || null;
  if (state === "lock") return { k: "nego", why: `마지막 실행에서 접근 차단${why ? ` · ${why}` : ""}`, src: "실행 기록", lane };
  if (run?.status === "failed") return { k: "dev", why: `실행기 실행 실패 (${run.run_id})`, src: "실행 기록", lane };
  if (run?.result?.site_status === "config_error") return { k: "dev", why: "설정 오류로 멈춘 실행", src: "실행 기록", lane };
  if (run) {
    const label = run.result?.site_status ? RESULT_STATUS_KO[run.result.site_status] ?? run.result.site_status : RUNNER_STATUS_KO[run.status] ?? run.status;
    return { k: "run", why: `실행 기록 있음 · ${label}`, src: "실행 기록", lane };
  }
  if (site?.source === "db_only") {
    return { k: "none", label: site.class_label || undefined, why: "판정 파일·실행 기록 없이 DB 문서만 있음", src: "DB 문서 수", lane };
  }
  if (!site || !site.state) return { k: site?.class || "none", why: "판정 기록 없음", src: "판정 파일 없음", lane };
  return {
    k: site.class || "none",
    why: stateLabel(site.state) + (site.detail ? ` · ${site.detail}` : ""),
    src: `site-status/${site.file}${site.updated_at ? ` (${site.updated_at})` : ""}`,
    lane,
  };
};

export type ConsoleInput = Readonly<{
  sites: unknown; uncollected: unknown; runs: unknown; health: unknown; stats: unknown; progress: unknown;
}>;

export type ConsoleData = Readonly<{
  sites: readonly ConsoleSite[];
  runner: RunnerStatus | null;
  runnerOk: boolean;
  runnerText: string;
  stats: StatsOverview | null;
  sitesOk: boolean;
  statusOk: boolean;
  documentsOk: boolean;
  runsOk: boolean;
  progressOk: boolean;
  /** O1 상태 파일(직접 실행 진행). 예전 BE 처럼 알 수 없으면 null. */
  directOk: boolean | null;
  uncollectedOk: boolean;
  latestJudgment: number | null;
  taxonomyVersion: string | null;
}>;

const rawRuns = (raw: unknown): unknown[] => (Array.isArray(raw) ? raw : []);

/** 사이트 목록 = /collect/sites ∪ /collect/uncollected ∪ /progress ∪ /collect/runs 의 site_id. */
export const buildConsole = (input: ConsoleInput, taxonomy: Taxonomy | null, names: ReadonlyMap<string, string>, now: number): ConsoleData => {
  const report = isRecord(input.sites) ? normalizeSitesReport(input.sites) : null;
  const rawSites = new Map(listOf(input.sites, "sites").flatMap((row) => isRecord(row) && str(row.site_id) ? [[str(row.site_id), row] as const] : []));
  const bySite = new Map((report?.sites ?? []).map((site) => [site.site_id, site]));
  const unc = new Map(normalizeUncollected(input.uncollected).map((row: UncollectedRow) => [row.site_id, row]));
  const uncCodes = new Map(listOf(input.uncollected, "sites").flatMap((row) =>
    isRecord(row) && str(row.site_id) && Array.isArray(row.uncollected_reasons) ? [[str(row.site_id), row.uncollected_reasons.map(str)] as const] : []));
  const backfill = new Map(normalizeProgress(input.progress).map((row) => [row.site_id, row]));
  const runsOk = Array.isArray(input.runs);
  const allRuns = runsOk ? normalizeRunnerRuns(input.runs) : [];
  const runsBySite = new Map<string, RunnerRun[]>();
  for (const run of allRuns) runsBySite.set(run.site_id, [...(runsBySite.get(run.site_id) ?? []), run]);
  // /collect/runs 를 못 읽었으면 /collect/sites 의 latest_run 이라도 쓴다.
  if (!runsOk) for (const [sid, row] of rawSites) {
    const latest = normalizeRunnerRun(row.latest_run);
    if (latest) runsBySite.set(sid, [latest]);
  }
  const runner = normalizeRunnerStatus(input.health);
  const runnerOk = runner ? runner.ok : report?.runner_ok === true;
  const reportReason = isRecord(input.sites) ? strOrNull(input.sites.runner_reason) : null;

  const ids = new Set<string>([...bySite.keys(), ...unc.keys(), ...backfill.keys(), ...runsBySite.keys()]);
  const tax = taxonomy?.sites ?? {};
  const sites: ConsoleSite[] = [...ids].sort().map((id) => {
    const site = bySite.get(id) ?? null;
    const runs = [...(runsBySite.get(id) ?? [])].sort((a, b) =>
      String(b.started_at ?? b.requested_at ?? b.run_id).localeCompare(String(a.started_at ?? a.requested_at ?? a.run_id)));
    // 실행 중·대기 작업이 있으면 그것을, 없으면 가장 최근 작업을 현재 상태로 쓴다(콘솔 규칙).
    const cur = runs.find((run) => ACTIVE.includes(run.status)) ?? runs[0] ?? null;
    const bf = backfill.get(id) ?? null;
    const direct = site?.direct ?? null;
    const st = stateOf(cur, bf, now, direct);
    const documents = site?.doc_count ?? bf?.docs_in_db ?? null;
    // 실행 중인 작업은 아직 결과가 없으므로, 결과가 있는 가장 최근 작업의 저장·실패 수를 쓴다.
    const withResult = runs.find((run) => run.result?.saved !== null && run.result?.saved !== undefined) ?? null;
    const runSaved = withResult?.result?.saved ?? null;
    const candidates: Array<[number | null, string]> = [[documents, "DB 문서 수"], [bf?.items_done ?? null, "백필 진행 건수"], [runSaved, "실행기 결과 저장 건수"],
      [direct?.saved ?? null, `직접 실행 저장 건수 (O1 상태${direct?.updated_at ? ` · ${direct.updated_at}` : ""})`]];
    const best = candidates.filter((c): c is [number, string] => c[0] !== null).sort((a, b) => b[0] - a[0])[0] ?? null;
    const saved = best?.[0] ?? 0;
    const total = bf?.total_estimate && bf.total_estimate >= saved ? bf.total_estimate : site?.plan?.expected_items ?? null;
    const totalSrc = total === null ? null : bf?.total_estimate === total ? "백필이 사이트에서 받은 총건수 추정" : "plan 예상 건수";
    const last = Math.max(tsOf(bf?.updated_at) ?? 0, tsOf(cur?.finished_at) ?? 0, tsOf(cur?.started_at) ?? 0,
      direct?.running ? tsOf(direct.updated_at) ?? 0 : 0);
    const u = unc.get(id);
    const codes = uncCodes.get(id) ?? [];
    const rawPlan = rawSites.get(id)?.plan;
    const t = tax[id];
    return {
      id,
      name: names.get(id) || id,
      type: t?.doc_type || UNCLASS,
      country: t?.country || UNCLASS,
      state: st.state, alert: st.alert, why: st.why,
      cls: classOf(site, cur, st.state, st.why),
      saved, savedSrc: best?.[1] ?? null,
      failed: withResult?.result?.failed ?? 0,
      total, totalSrc,
      lastTs: last > 0 ? last : null,
      documents,
      plan: site?.plan ?? null,
      planInfo: isRecord(rawPlan) ? { strategy: strOrNull(rawPlan.strategy), folder: strOrNull(rawPlan.folder) } : null,
      runs, backfill: bf,
      status: site && site.state ? { state: site.state, detail: site.detail, codes: site.reason_codes, file: site.file, updated_at: site.updated_at } : null,
      approval: site !== null && needsApproval(site),
      uncollected: u ? { reasons: codes.length ? codes.map((code) => UNCOLLECTED_LABELS[code] ?? code) : u.reason ? [u.reason] : [], note: u.note } : null,
    };
  });

  const judged = (report?.sites ?? []).map((site) => tsOf(site.updated_at) ?? 0);
  const latestJudgment = judged.length ? Math.max(...judged) : 0;
  return {
    sites, runner, runnerOk, runnerText: runnerOk ? "" : runnerDownText(runner, reportReason),
    stats: normalizeStats(input.stats),
    sitesOk: report !== null,
    statusOk: report?.status_dir_ok === true,
    documentsOk: isRecord(input.sites) && input.sites.documents_ok === true,
    runsOk,
    progressOk: isRecord(input.progress) && Array.isArray(input.progress.sites),
    directOk: report?.progress_ok ?? null,
    uncollectedOk: isRecord(input.uncollected) && Array.isArray(input.uncollected.sites),
    latestJudgment: latestJudgment > 0 ? latestJudgment : null,
    taxonomyVersion: taxonomy?.version ?? null,
  };
};

// ---- 필터(콘솔 scoped / filtered) ---------------------------------------------

export const needsAttention = (site: ConsoleSite): boolean => site.state === "stop" || site.state === "review";

export type Scope = Readonly<{
  attn: boolean; type: string; country: string; cls: string;
  /** 판정 파일 구역(예: "L2a"). 예전 사이트 수집 표의 구역 필터. */
  lane?: string;
  /** 콘솔에 없던 운영 보기: "approval"(승인 대기) | "uncollected"(미수집) | "" */
  special: "" | "approval" | "uncollected";
}>;
export type Filter = Scope & Readonly<{ state: string; q: string }>;

export const scoped = (list: readonly ConsoleSite[], scope: Scope): ConsoleSite[] => list.filter((site) =>
  (!scope.attn || needsAttention(site)) &&
  (!scope.type || site.type === scope.type) &&
  (!scope.country || site.country === scope.country) &&
  (!scope.cls || site.cls.k === scope.cls) &&
  (!scope.lane || site.cls.lane === scope.lane) &&
  (scope.special !== "approval" || site.approval) &&
  (scope.special !== "uncollected" || site.uncollected !== null));

export const filtered = (list: readonly ConsoleSite[], filter: Filter): ConsoleSite[] => {
  const q = filter.q.trim().toLowerCase();
  return scoped(list, filter).filter((site) =>
    (!filter.state || site.state === filter.state) &&
    (!q || `${site.id} ${site.name}`.toLowerCase().includes(q)));
};

export const laneNames = (list: readonly ConsoleSite[]): string[] =>
  [...new Set(list.map((site) => site.cls.lane).filter((lane): lane is string => Boolean(lane)))].sort();

/** 콘솔 정렬: 상태 순서 → 규모(총건수 또는 저장) 큰 순 → ID. */
export const sortSites = (list: readonly ConsoleSite[]): ConsoleSite[] => [...list].sort((a, b) =>
  STATE_ORDER[a.state] - STATE_ORDER[b.state] || (b.total ?? b.saved) - (a.total ?? a.saved) || a.id.localeCompare(b.id));

/** 콘솔 namesBy: 많은 순, 미분류는 맨 뒤. */
export const namesBy = (list: readonly ConsoleSite[], field: "type" | "country"): string[] => {
  const counts = new Map<string, number>();
  for (const site of list) counts.set(site[field], (counts.get(site[field]) ?? 0) + 1);
  return [...counts.keys()].sort((a, b) => Number(a === UNCLASS) - Number(b === UNCLASS) || (counts.get(b) ?? 0) - (counts.get(a) ?? 0));
};

export const countStates = (list: readonly ConsoleSite[]): Record<ConsoleState, number> => {
  const out = { run: 0, stop: 0, review: 0, done: 0, lock: 0, idle: 0 };
  for (const site of list) out[site.state] += 1;
  return out;
};

export const countClasses = (list: readonly ConsoleSite[]): Record<ClassKey, number> => {
  const out = { run: 0, dev: 0, nego: 0, skip: 0, none: 0 };
  for (const site of list) out[site.cls.k] += 1;
  return out;
};

/** 보조 칩: 기본 축(유형/국가)으로 좁힌 뒤 반대 축 이름별 개수, 많은 순. */
export const secondaryCounts = (list: readonly ConsoleSite[], field: "type" | "country"): Array<[string, number]> => {
  const counts = new Map<string, number>();
  for (const site of list) counts.set(site[field], (counts.get(site[field]) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
};

/** 10만 건이 넘는 사이트만 진행 막대(콘솔 규칙). 총건수 모르면 비율 없음. */
export const BIG_SITE = 100_000;
export const isBig = (site: ConsoleSite): boolean => (site.total ?? 0) >= BIG_SITE || site.saved >= BIG_SITE;
export const percentOf = (site: ConsoleSite): number | null => (site.total ? Math.min(100, (site.saved / site.total) * 100) : null);

// ---- 시작 전 점검(콘솔 startBox / pre) -----------------------------------------

export type StartCheck = Readonly<{ blocked: readonly string[]; resumeBlocked: readonly string[]; liveRun: RunnerRun | null }>;

/** 시작·이어받기를 막을 이유. blocked 는 두 버튼 모두, resumeBlocked 는 이어받기만 막는다. */
export const startCheck = (site: ConsoleSite, runner: RunnerStatus | null, runnerOk: boolean, runnerText: string): StartCheck => {
  const no: string[] = [];
  const live = site.runs.find((run) => run.status === "queued" || run.status === "running" || run.status === "cancelling") ?? null;
  if (!runnerOk) no.push(`실행기 없음 — ${runnerText || "연결 안 됨"}`);
  else if (runner?.paused) no.push("실행기가 일시 중지돼 있어 새 작업을 받지 않습니다");
  if (!site.plan) no.push(`실행기에 등록된 plan 이 없습니다 (${CLASSES[site.cls.k].label} · ${site.cls.why})`);
  else if (!site.plan.ready) no.push(`plan 등록됨, 실행 불가 — ${site.plan.why || "이유 미기재"}`);
  if (live) no.push(`실행기 작업 ${live.run_id} 이(가) ${live.status === "queued" ? "대기 중" : live.status === "cancelling" ? "취소 중" : "실행 중"}입니다`);
  else if (site.state === "run") no.push("이미 수집 중입니다");
  if (site.state === "lock" || site.cls.k === "nego") no.push("접근 제한·협의 필요 사이트는 시작할 수 없습니다");
  const resume = site.approval
    ? [`승인 대기 — 예상 ${site.plan?.expected_items === null || site.plan?.expected_items === undefined ? "미상" : `${fmt(site.plan.expected_items)}건`}: 1만 건 이상이거나 모르면 실행기 등록 때 대량 승인(--approved-large)이 있어야 이어받기를 받습니다. 시험 수집은 승인 없이 됩니다`]
    : [];
  return { blocked: no, resumeBlocked: resume, liveRun: live };
};

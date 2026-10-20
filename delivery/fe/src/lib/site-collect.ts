// 사이트 수집 화면: BE /collect/* 응답 모양과 화면 판단을 한곳에 둔다.
//
// 이 화면은 예전 운영 콘솔(plan-dev-20261008/dashboard)의 사이트 표·시작
// 버튼을 옮긴 것이다. BE 쪽 필드 이름은 아직 바뀔 수 있어서, 응답은 모두
// normalize* 를 거쳐 들어오고 빠진 필드는 null/빈 값으로 채운다. 필드 이름이
// 바뀌면 이 파일의 normalize* 만 고치면 된다.

export type CollectClass = "run" | "dev" | "nego" | "skip" | "";

export type CollectRun = Readonly<{
  run_id: string;
  site_id: string;
  mode: string | null;
  status: string;
  started: string | null;
  finished: string | null;
  result: string | null;
  done: number | null;
  total: number | null;
}>;

/** O1 상태 파일(status/O1-full.csv) 한 줄: 실행기 밖에서 직접 돌리는 수집의 진행. */
export type DirectProgress = Readonly<{
  status: string;
  running: boolean;
  saved: number | null;
  run_folder: string | null;
  started_at: string | null;
  updated_at: string | null;
  detail: string;
}>;

export type CollectPlan = Readonly<{ ready: boolean; why: string; expected_items: number | null; approved_large: boolean }>;

export type CollectSite = Readonly<{
  site_id: string;
  state: string;
  class: CollectClass;
  detail: string;
  reason_codes: readonly string[];
  updated_at: string | null;
  lane: string;
  file: string;
  doc_count: number | null;
  expected: number | null;
  plan: CollectPlan | null;
  latest_run: CollectRun | null;
  /** "status"(판정 파일) | "runner"(실행기 기록) | "db_only"(DB 문서만 있음) | "" (모름) */
  source: string;
  /** BE 가 정한 구분 이름(DB 에만 있는 사이트는 "판정 없음"·"API 수집" 등). */
  class_label: string;
  direct: DirectProgress | null;
}>;

export type CollectSitesReport = Readonly<{
  sites: readonly CollectSite[];
  runner_ok: boolean;
  status_dir_ok: boolean;
  /** O1 상태 파일을 읽었는지. 필드가 없는 예전 BE 면 null. */
  progress_ok: boolean | null;
}>;

export type UncollectedRow = Readonly<{ site_id: string; class: CollectClass; reason: string; note: string }>;

export type RunnerHealth = Readonly<{ ok: boolean; paused: boolean; detail: string }>;

type Raw = Record<string, unknown>;

const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string => (typeof value === "string" ? value : typeof value === "number" ? String(value) : "");
const strOrNull = (value: unknown): string | null => str(value) || null;
const num = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
};
const firstNum = (source: Raw, keys: readonly string[]): number | null => {
  for (const key of keys) {
    const value = num(source[key]);
    if (value !== null) return value;
  }
  return null;
};
const listOf = (raw: unknown, keys: readonly string[]): readonly unknown[] => {
  if (Array.isArray(raw)) return raw;
  if (!isRecord(raw)) return [];
  for (const key of keys) if (Array.isArray(raw[key])) return raw[key] as unknown[];
  return [];
};

const CLASSES: readonly CollectClass[] = ["run", "dev", "nego", "skip"];
const toClass = (value: unknown): CollectClass => (CLASSES.includes(value as CollectClass) ? value as CollectClass : "");

const DONE_KEYS = ["saved", "done", "collected", "doc_count", "count", "items_saved"] as const;
const TOTAL_KEYS = ["total", "expected", "reported_total", "target"] as const;

export const normalizeRun = (raw: unknown): CollectRun | null => {
  if (!isRecord(raw)) return null;
  const runId = str(raw.run_id ?? raw.id);
  if (!runId) return null;
  // 진행 값은 실행기가 주는 그대로 온다: progress 객체 안이거나 실행 행 바로 위.
  const progress = isRecord(raw.progress) ? raw.progress : {};
  const items = isRecord(progress.items) ? progress.items : isRecord(raw.items) ? raw.items : {};
  // 실행기 result: {site_status, stop_reason, saved, failed} 또는 {error}.
  const result = isRecord(raw.result) ? raw.result : {};
  const resultText = isRecord(raw.result)
    ? [str(result.error), str(result.site_status), str(result.stop_reason)].filter(Boolean).join(" · ")
    : str(raw.result ?? raw.stop_reason);
  return {
    run_id: runId,
    site_id: str(raw.site_id),
    mode: strOrNull(raw.mode),
    status: str(raw.status) || "unknown",
    started: strOrNull(raw.started ?? raw.started_at),
    finished: strOrNull(raw.finished ?? raw.finished_at),
    result: [resultText, str(raw.reason)].filter(Boolean).join(" · ") || null,
    done: firstNum(result, DONE_KEYS) ?? firstNum(progress, DONE_KEYS) ?? firstNum(items, DONE_KEYS) ?? firstNum(raw, DONE_KEYS),
    total: firstNum(progress, TOTAL_KEYS) ?? firstNum(raw, TOTAL_KEYS),
  };
};

export const normalizeDirect = (raw: unknown): DirectProgress | null => {
  if (!isRecord(raw) || !str(raw.status)) return null;
  return {
    status: str(raw.status), running: raw.running === true, saved: num(raw.saved),
    run_folder: strOrNull(raw.run_folder), started_at: strOrNull(raw.started_at), updated_at: strOrNull(raw.updated_at),
    detail: str(raw.detail),
  };
};

export const normalizeSite = (raw: unknown): CollectSite | null => {
  if (!isRecord(raw)) return null;
  const siteId = str(raw.site_id).trim();
  if (!siteId) return null;
  const latest = normalizeRun(raw.latest_run);
  const plan = isRecord(raw.plan) ? raw.plan : null;
  const codes = Array.isArray(raw.reason_codes) ? raw.reason_codes.map(str).filter(Boolean)
    : typeof raw.reason_codes === "string" ? raw.reason_codes.split(/[,;|\s]+/).filter(Boolean) : [];
  return {
    site_id: siteId,
    state: str(raw.state),
    class: toClass(raw.class ?? raw.cls),
    detail: str(raw.detail),
    reason_codes: codes,
    updated_at: strOrNull(raw.updated_at),
    // BE 는 lane 을 따로 주지 않는다: 판정 파일 이름(예: "L2a.csv")이 곧 구역이다.
    lane: str(raw.lane) || str(raw.status_file).replace(/\.csv$/i, ""),
    file: str(raw.file ?? raw.status_file),
    doc_count: num(raw.doc_count ?? raw.documents),
    expected: firstNum(raw, ["expected", "expected_total", "reported_total", "total"])
      ?? (plan ? num(plan.expected_items) : null) ?? latest?.total ?? null,
    plan: plan ? { ready: plan.ready === true, why: str(plan.why), expected_items: num(plan.expected_items), approved_large: plan.approved_large === true } : null,
    latest_run: latest,
    source: str(raw.source),
    class_label: str(raw.class_label),
    direct: normalizeDirect(raw.direct),
  };
};

export const normalizeSitesReport = (raw: unknown): CollectSitesReport => ({
  sites: listOf(raw, ["sites", "rows", "items"]).map(normalizeSite).filter((site): site is CollectSite => site !== null),
  runner_ok: isRecord(raw) && raw.runner_ok === true,
  // BE 초안은 status_source.ok 로 준다.
  status_dir_ok: isRecord(raw) && (raw.status_dir_ok === true || (isRecord(raw.status_source) && raw.status_source.ok === true)),
  progress_ok: isRecord(raw) && isRecord(raw.progress_source) ? raw.progress_source.ok === true : null,
});

// /collect/uncollected 는 reason 문자열 대신 uncollected_reasons 코드 목록을 줄 수 있다.
const UNCOLLECTED_REASON_LABELS: Readonly<Record<string, string>> = {
  class_dev: "개발 필요", class_nego: "협의 필요", class_skip: "대상 아님", class_none: "판정 없음", no_documents: "수집 문서 0건",
};

export const normalizeUncollected = (raw: unknown): readonly UncollectedRow[] =>
  listOf(raw, ["sites", "rows", "items"]).flatMap((row) => {
    if (!isRecord(row) || !str(row.site_id)) return [];
    const codes = Array.isArray(row.uncollected_reasons) ? row.uncollected_reasons.map(str).filter(Boolean) : [];
    const reason = str(row.reason) || codes.map((code) => UNCOLLECTED_REASON_LABELS[code] ?? code).join(" · ");
    return [{ site_id: str(row.site_id), class: toClass(row.class), reason, note: str(row.note) }];
  });

export const normalizeRunnerHealth = (raw: unknown): RunnerHealth | null => {
  if (!isRecord(raw)) return null;
  return {
    ok: raw.ok === true || raw.runner_ok === true || raw.status === "ok",
    paused: raw.paused === true || raw.status === "paused",
    detail: str(raw.detail ?? raw.reason ?? raw.error),
  };
};

// ---- 표시 문구 ------------------------------------------------------------

export const CLASS_LABELS: Readonly<Record<string, string>> = {
  run: "실행 가능", dev: "개발 필요", nego: "협의 필요", skip: "대상 아님", "": "판정 없음",
};
export const classLabel = (value: CollectClass): string => CLASS_LABELS[value] ?? "판정 없음";

// site-status 판정 파일의 state 값 (예전 콘솔의 SS 표와 같은 뜻).
const STATE_LABELS: Readonly<Record<string, string>> = {
  ready: "plan·시험 수집 통과", plan_written: "plan 작성됨 · 시험 수집 전", needs_dev: "수집기 개발 필요",
  blocked_access: "접근 제한", moved: "주소 이전 · plan 다시 작성", no_crawler: "수집기 없음", skip: "대상 아님",
};
export const stateLabel = (state: string): string => (state ? STATE_LABELS[state] ?? state : "판정 기록 없음");

const RUN_STATUS_LABELS: Readonly<Record<string, string>> = {
  queued: "대기 중", running: "실행 중", done: "완료", cancelling: "취소 중", cancelled: "취소됨", succeeded: "완료",
  complete: "완료", completed: "완료", complete_with_review: "완료(검토 필요)", complete_with_mismatch: "완료(총건수 불일치)",
  incomplete: "미완료", failed: "실패", config_error: "설정 오류", access_blocked: "접근 차단",
  needs_approval: "승인 대기", pending_approval: "승인 대기",
};
export const runStatusLabel = (status: string): string => RUN_STATUS_LABELS[status] ?? status;
export const runModeLabel = (mode: string | null): string => (mode === "trial" ? "시험 수집" : mode === "resume" ? "이어받기" : mode ?? "");
export const isRunActive = (run: CollectRun | null): boolean =>
  run !== null && ["queued", "running", "cancelling"].includes(run.status);

// POST /collect/runs 가 409 로 돌려주는 reason 값.
export const REJECTION_LABELS: Readonly<Record<string, string>> = {
  plan_not_registered: "실행기에 등록된 plan이 없습니다. plan을 먼저 등록해야 합니다.",
  plan_not_ready: "plan은 등록됐지만 아직 실행할 수 없는 상태입니다.",
  bad_limit: "수집 건수 값이 올바르지 않습니다.",
  large_needs_approval: "대량 수집이라 승인이 필요합니다. 승인 대기 목록에서 확인하세요.",
  cross_origin: "이 화면 밖에서 보낸 요청이라 거절했습니다.",
  unit_reserved: "이 수집 단위는 다른 작업에 예약돼 있습니다.",
  site_busy: "이 사이트는 이미 대기 중이거나 실행 중인 작업이 있습니다.",
  unit_busy: "같은 수집 단위의 다른 작업이 실행 중입니다.",
  folder_busy: "같은 실행 폴더를 다른 작업이 쓰고 있습니다.",
  external_unknown: "실행기 밖에서 같은 사이트 수집이 돌고 있는지 확인할 수 없습니다.",
  docker_unavailable: "수집 서버의 Docker를 쓸 수 없습니다.",
  runner_paused: "실행기가 일시 중지돼 있어 새 작업을 받지 않습니다.",
  be_unreachable: "수집 BE에 연결할 수 없습니다.",
  runner_unavailable: "실행기에 연결할 수 없습니다.",
  runner_unconfigured: "BE에 실행기 주소나 토큰이 설정돼 있지 않습니다.",
  runner_auth_failed: "실행기가 BE의 토큰을 거부했습니다.",
  runner_bad_response: "실행기 응답을 해석할 수 없습니다.",
  runner_rejected: "실행기가 요청을 거절했습니다.",
  bad_site_id: "사이트 ID 형식이 올바르지 않습니다.",
  bad_mode: "지원하지 않는 수집 모드입니다.",
  bad_run_id: "작업 ID 형식이 올바르지 않습니다.",
  bad_request: "요청 값이 올바르지 않습니다.",
  not_found: "실행기에 없는 작업입니다.",
  not_active: "이미 끝났거나 취소된 작업이라 취소할 수 없습니다.",
};

export type Rejection = Readonly<{ reason: string; message: string }>;

export const describeRejection = (status: number, body: unknown): Rejection => {
  const record = isRecord(body) ? body : {};
  const nested = isRecord(record.detail) ? record.detail : null;
  const reason = str(record.reason) || str(nested?.reason) || (typeof record.detail === "string" && REJECTION_LABELS[record.detail] ? record.detail : "");
  const detail = nested ? str(nested.detail) : str(record.detail);
  const known = REJECTION_LABELS[reason];
  const extra = detail && detail !== reason ? ` (${detail})` : "";
  if (known) return { reason, message: `${known}${extra}` };
  const head = reason ? `거절됨: ${reason}` : `요청이 거절됐습니다`;
  return { reason, message: `${head}${extra} · 상태 코드 ${status}` };
};

// ---- 승인 대기 ------------------------------------------------------------

export const LARGE_RUN_ITEMS = 10_000;

// 실행기 규칙과 같다: 이어받기(전체) 실행은 대량 승인(approved_large)이 없고 예상 건수가
// 모르거나 10000건 이상이면 승인이 필요하다. 시험 수집은 해당하지 않는다.
export const needsApproval = (site: CollectSite): boolean =>
  site.plan !== null && !site.plan.approved_large &&
  (site.plan.expected_items === null || site.plan.expected_items >= LARGE_RUN_ITEMS);

// ---- 필터 ----------------------------------------------------------------

export type SiteFilter = Readonly<{ cls: string; lane: string; q: string }>;
export const UNCLASSIFIED = "unclassified";

export const filterSites = (sites: readonly CollectSite[], filter: SiteFilter, reasons?: ReadonlyMap<string, UncollectedRow>): CollectSite[] => {
  const q = filter.q.trim().toLowerCase();
  return sites.filter((site) => {
    if (filter.cls === UNCLASSIFIED ? site.class !== "" : filter.cls && site.class !== filter.cls) return false;
    if (filter.lane && site.lane !== filter.lane) return false;
    if (!q) return true;
    const reason = reasons?.get(site.site_id);
    return [site.site_id, site.lane, site.state, site.detail, ...site.reason_codes, reason?.reason ?? "", reason?.note ?? ""]
      .join(" ").toLowerCase().includes(q);
  });
};

export const laneOptions = (sites: readonly CollectSite[]): string[] =>
  [...new Set(sites.map((site) => site.lane).filter(Boolean))].sort();

export const countByClass = (sites: readonly CollectSite[]): Record<string, number> =>
  sites.reduce<Record<string, number>>((counts, site) => {
    const key = site.class || UNCLASSIFIED;
    counts[key] = (counts[key] ?? 0) + 1;
    return counts;
  }, {});

export const progressPercent = (done: number | null, total: number | null): number | null =>
  done === null || !total || total <= 0 ? null : Math.min(100, (done / total) * 100);

// ---- 실행 요청 -------------------------------------------------------------

export const TRIAL_LIMIT_MAX = 1000;

export type CollectAction =
  | Readonly<{ kind: "trial"; siteId: string; limit: number }>
  | Readonly<{ kind: "resume"; siteId: string }>
  | Readonly<{ kind: "cancel"; siteId: string; runId: string }>;

export const confirmMessage = (action: CollectAction): string => {
  if (action.kind === "trial") return `${action.siteId}\n시험 수집 ${action.limit.toLocaleString("ko-KR")}건을 실행기에 요청할까요?`;
  if (action.kind === "resume") return `${action.siteId}\n수집(이어받기·전체)을 실행기에 요청할까요?\n대량 수집이면 승인 전까지 시작되지 않습니다.`;
  return `${action.siteId}\n작업 ${action.runId}을(를) 취소할까요?`;
};

export type ActionOutcome =
  | Readonly<{ kind: "skipped" }>
  | Readonly<{ kind: "ok"; message: string; runId: string | null }>
  | Readonly<{ kind: "rejected"; reason: string; message: string }>;

export type ActionDeps = Readonly<{ confirm: (message: string) => boolean; fetch: typeof fetch }>;

// 확인 창에서 "확인"을 누르기 전에는 어떤 요청도 보내지 않는다.
export const performAction = async (action: CollectAction, deps: ActionDeps): Promise<ActionOutcome> => {
  if (!deps.confirm(confirmMessage(action))) return { kind: "skipped" };
  const request = action.kind === "cancel"
    ? { url: `/api/collect/runs/${encodeURIComponent(action.runId)}/cancel`, body: {} }
    : { url: "/api/collect/runs", body: { siteId: action.siteId, mode: action.kind, ...(action.kind === "trial" ? { limit: action.limit } : {}) } };
  try {
    const response = await deps.fetch(request.url, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(request.body),
    });
    const body: unknown = await response.json().catch(() => ({}));
    if (!response.ok) return { kind: "rejected", ...describeRejection(response.status, body) };
    const runId = isRecord(body) ? strOrNull(body.run_id) : null;
    const cancelled = isRecord(body) && body.status === "cancelled";
    const message = action.kind === "cancel"
      ? cancelled ? `작업 ${action.runId}을(를) 취소했습니다.` : `작업 ${action.runId} 취소를 요청했습니다. 다음 확인 지점에서 멈춥니다.`
      : `요청됨${runId ? ` · 작업 ${runId}` : ""}`;
    return { kind: "ok", message, runId };
  } catch (error) {
    return { kind: "rejected", reason: "be_unreachable", message: `요청을 보내지 못했습니다 (${error instanceof Error ? error.message : String(error)})` };
  }
};

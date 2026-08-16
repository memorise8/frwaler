import { UNBOUNDED_CRAWL_LIMIT } from "@/lib/crawl-limit";

// The catalogue holds 804 crawlers; the cap sits just above it so a whole-fleet
// sweep is expressible while a runaway or hand-crafted request is not.
export const MAX_BULK_SITES = 1000;
export const BULK_LIMIT_MIN = 1;
export const BULK_LIMIT_MAX = 1000;
export const DEFAULT_BULK_LIMIT = 20;

// Matches the backend's own site_id pattern (JobIn, delivery/be/app.py) so a
// malformed id is refused here instead of costing one round trip per site.
const SITE_ID_PATTERN = /^[A-Za-z0-9._-]{1,200}$/;

export type BulkMode = "incremental" | "full";

export type BulkRunRequest = Readonly<{
  siteIds: readonly string[];
  mode: BulkMode;
  limit: number;
}>;

export type BulkParseResult =
  | Readonly<{ ok: true; value: BulkRunRequest }>
  | Readonly<{ ok: false; error: string }>;

// A bulk run always carries a per-site cap. Unbounded collection stays a
// single-site decision: one operator confirmation cannot meaningfully cover
// "every document on 804 third-party sites", and the resulting queue would
// have no predictable end. The sentinel is rejected explicitly rather than
// falling through to the numeric check, so the refusal names the real reason.
export const parseBulkRunRequest = (body: unknown): BulkParseResult => {
  if (typeof body !== "object" || body === null) return { ok: false, error: "요청 형식이 올바르지 않습니다." };
  const input = body as Record<string, unknown>;

  if (!Array.isArray(input.siteIds)) return { ok: false, error: "대상 사이트 목록이 없습니다." };
  const raw = input.siteIds;
  if (raw.some((item) => typeof item !== "string")) return { ok: false, error: "대상 사이트 목록에 문자열이 아닌 값이 있습니다." };
  const siteIds = [...new Set((raw as string[]).map((item) => item.trim()).filter(Boolean))];
  if (siteIds.length === 0) return { ok: false, error: "대상 사이트가 한 개도 없습니다." };
  if (siteIds.length > MAX_BULK_SITES) {
    return { ok: false, error: `한 번에 실행할 수 있는 사이트는 최대 ${MAX_BULK_SITES.toLocaleString("ko-KR")}개입니다.` };
  }
  const malformed = siteIds.find((item) => !SITE_ID_PATTERN.test(item));
  if (malformed !== undefined) return { ok: false, error: `사이트 ID 형식이 올바르지 않습니다: ${malformed}` };

  const mode = input.mode === "full" ? "full" : input.mode === "incremental" || input.mode === undefined ? "incremental" : null;
  if (mode === null) return { ok: false, error: "수집 모드는 증분 또는 전체만 가능합니다." };

  if (input.limit === UNBOUNDED_CRAWL_LIMIT) {
    return { ok: false, error: "일괄 실행에서는 무제한 수집을 사용할 수 없습니다. 사이트당 최대 건수를 지정해 주세요." };
  }
  const limit = typeof input.limit === "number" ? input.limit : Number(input.limit);
  if (!Number.isInteger(limit) || limit < BULK_LIMIT_MIN || limit > BULK_LIMIT_MAX) {
    return { ok: false, error: `사이트당 최대 건수는 ${BULK_LIMIT_MIN}~${BULK_LIMIT_MAX.toLocaleString("ko-KR")} 사이의 정수여야 합니다.` };
  }

  return { ok: true, value: { siteIds, mode, limit } };
};

export type BulkOutcome = Readonly<
  | { siteId: string; kind: "queued"; jobId: number }
  | { siteId: string; kind: "skipped"; reason: string }
  | { siteId: string; kind: "failed"; reason: string }
>;

// 409 is not an error here. The backend allows one active job per site
// (ActiveJobError), so a site already collecting is simply left alone -- which
// is what makes re-running a bulk sweep safe rather than duplicating work.
export const classifyBulkOutcome = (siteId: string, status: number, payload: unknown): BulkOutcome => {
  const body = (typeof payload === "object" && payload !== null ? payload : {}) as Record<string, unknown>;
  if (status === 409) return { siteId, kind: "skipped", reason: "이미 실행 중인 작업이 있습니다." };
  if (status === 404) return { siteId, kind: "failed", reason: "백엔드에 등록되지 않은 사이트입니다." };
  if (status >= 200 && status < 300) {
    const jobId = Number(body.id);
    if (!Number.isInteger(jobId)) return { siteId, kind: "failed", reason: "작업 번호를 받지 못했습니다." };
    return { siteId, kind: "queued", jobId };
  }
  const detail = typeof body.detail === "string" ? body.detail : `HTTP ${status}`;
  return { siteId, kind: "failed", reason: detail };
};

export type BulkSummary = Readonly<{ queued: number; skipped: number; failed: number }>;

export const summarizeBulkRun = (outcomes: readonly BulkOutcome[]): BulkSummary => ({
  queued: outcomes.filter((item) => item.kind === "queued").length,
  skipped: outcomes.filter((item) => item.kind === "skipped").length,
  failed: outcomes.filter((item) => item.kind === "failed").length,
});

export const describeBulkRun = (summary: BulkSummary): string => {
  const parts = [`등록 ${summary.queued.toLocaleString("ko-KR")}건`];
  if (summary.skipped > 0) parts.push(`건너뜀 ${summary.skipped.toLocaleString("ko-KR")}건(이미 실행 중)`);
  if (summary.failed > 0) parts.push(`실패 ${summary.failed.toLocaleString("ko-KR")}건`);
  return parts.join(" · ");
};

export type CancelSummary = Readonly<{ cancelled: number; requested: number; failed: number }>;

// A queued job cancels outright; a running one only receives the request and
// stops at the crawler's next cooperative checkpoint (jobs.cancel_job). The two
// must be reported separately -- telling an operator that a running crawl is
// "cancelled" when it is still fetching pages would be a lie they act on.
export const describeBulkCancel = (summary: CancelSummary): string => {
  const parts: string[] = [];
  if (summary.cancelled > 0) parts.push(`대기 작업 ${summary.cancelled.toLocaleString("ko-KR")}건 취소`);
  if (summary.requested > 0) parts.push(`실행 중 ${summary.requested.toLocaleString("ko-KR")}건은 현재 수집 단위가 끝나면 중단됩니다`);
  if (summary.failed > 0) parts.push(`실패 ${summary.failed.toLocaleString("ko-KR")}건`);
  return parts.length ? parts.join(" · ") : "취소할 작업이 없습니다.";
};

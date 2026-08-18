// The backend's crawl_jobs.status column (delivery/worker/jobs.py) only ever
// settles into "done", "failed", or "cancelled" -- "queued", "running", and
// "cancelling" are all still in flight. Polling code needs a single place to
// ask "is this worth fetching again?" so that decision cannot drift out of
// sync between the run panel and any other screen that watches a job.
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["done", "failed", "cancelled"]);

export const isTerminalJobStatus = (status: string): boolean => TERMINAL_STATUSES.has(status);

export const CRAWL_JOB_STATUS_LABELS: Readonly<Record<string, string>> = {
  queued: "대기",
  running: "실행 중",
  cancelling: "취소 처리 중",
  done: "완료",
  failed: "실패",
  cancelled: "취소됨",
};

export const jobStatusLabel = (status: string): string => CRAWL_JOB_STATUS_LABELS[status] ?? status;

// How often the run panel re-fetches a tracked job. Fast enough to feel live
// for the (typically few-second) jobs this screen submits, slow enough not
// to hammer the BE while a longer full crawl is still running.
export const JOB_POLL_INTERVAL_MS = 3000;

export type JobOutcomeInput = Readonly<{
  status: string;
  saved_count: number | null | undefined;
  error?: string | null;
  truncated?: boolean | null;
}>;

// status 를 대체하지 않고 "완료" 옆에 붙는다. 잘린 실행도 저장한 문서는 유효하므로
// 실패가 아니고, 그렇다고 다 받은 것도 아니다.
export const TRUNCATED_BADGE = "부분 수집";

// saved_count is the net increase in rows for the site (delivery/README.md,
// "4. 수집 결과 읽는 법"): re-processing already-collected documents updates
// existing rows without adding new ones, so a run that did real work can
// still report 0. The crawler's own processed-count ("Total saved: N") only
// ever reaches container logs -- no API exposes it -- so this text must not
// claim to know it, only explain why 0 is not evidence of failure.
export const describeJobOutcome = (job: JobOutcomeInput): string => {
  if (job.status === "failed") {
    return job.error
      ? `작업이 실패했습니다: ${job.error}`
      : "작업이 실패했습니다. 실패 사유가 기록되지 않았습니다.";
  }
  if (job.status === "cancelled") {
    return "작업이 취소되었습니다.";
  }
  if (job.status === "done") {
    const saved = job.saved_count ?? 0;
    // 재실행하면 이어받는다고 쓰지 않는다. 확인한 사실: 800개 중 이미 받은 것을
    // 건너뛰는 장치를 가진 크롤러는 사실상 없다(has_blob 0개, _last_save_created 0개,
    // pdf_downloaded 조회 10개). 재실행은 1페이지부터 다시 걸어 같은 자리에서 또
    // 잘린다. 실제 해결책은 워커의 LIBERTREE_MAX_WALL_S 를 늘리는 것뿐이다.
    const cut = job.truncated
      ? "시간 제한(기본 25분)에 걸려 남은 페이지를 건너뛰었습니다. 다시 실행해도 크롤러가 처음부터 다시 훑기 때문에 같은 지점에서 멈춥니다. "
        + "이 사이트를 끝까지 받으려면 워커의 시간 제한을 늘려야 합니다(LIBERTREE_MAX_WALL_S). "
      : "";
    if (saved === 0) {
      return cut + "신규 저장 0건입니다. DB에 새로 추가된 문서가 없다는 뜻이며 실패가 아닙니다. "
        + "이미 수집된 문서만 다시 처리했을 수 있습니다. 크롤러가 실제로 처리한 문서 수는 이 화면에서 확인할 수 없습니다.";
    }
    return cut + `신규 저장 ${saved.toLocaleString("ko-KR")}건입니다.`;
  }
  return "";
};

// Distinguishes "the job we were tracking is gone" (BE says 404 -- it was
// purged, or the id was wrong) from any other polling failure (network
// error, BE down, unexpected status). Both must stop polling and say so
// plainly rather than leaving the panel silently stuck on its last known
// "실행 중" text.
export const describeTrackingFailure = (status: number | null): string =>
  status === 404
    ? "작업 기록을 찾을 수 없습니다. 작업 현황에서 다시 확인하세요."
    : "작업 상태를 확인할 수 없습니다. 잠시 후 다시 시도하세요.";

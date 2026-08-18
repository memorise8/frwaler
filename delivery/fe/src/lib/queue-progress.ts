// Turns the backend's queue summary into the two sentences an operator needs
// while a 804-site sweep runs for hours: how much is left, and how long that
// will take. Kept free of React and fetch so it can be tested -- this repo's
// vitest has no jsdom, so anything living inside a client component is
// untestable by construction.

export type QueueProgress = Readonly<{
  queued: number;
  running: number;
  active: number;
  finished: number;
  etaText: string | null;
  headline: string;
}>;

const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;

// Counts are whole things. A fractional "784.5건" is a malformed payload, not
// a rounding difference worth tolerating.
const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

// A mean duration is legitimately fractional -- the backend computes it with
// Postgres avg(...)::float8 -- so it gets its own predicate. Squeezing it
// through isCount would reject a perfectly good 22.4 and silently drop the
// estimate.
const isDuration = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

// The worker is a single serial loop -- one site at a time -- so the wait is
// simply the queue depth times how long one site has been taking. That is
// optimistic when the translation queue is non-empty: run_cycle also spends
// one cycle per loop on a translation job, and avg_seconds measures only
// crawl duration, so it does not account for that time.
export const formatEta = (seconds: number | null, activeJobs: number): string | null => {
  if (seconds === null || !isDuration(seconds) || !isCount(activeJobs) || activeJobs === 0) return null;
  const totalSeconds = Math.round(seconds * activeJobs);
  if (totalSeconds < SECONDS_PER_MINUTE) return "1분 미만 남음";
  // Round to whole minutes BEFORE splitting. Splitting first and rounding the
  // remainder lets the minute component reach 60: an ordinary 22-second mean
  // with 327 jobs left rendered "약 1시간 60분 남음".
  const totalMinutes = Math.round(totalSeconds / SECONDS_PER_MINUTE);
  if (totalMinutes < MINUTES_PER_HOUR) return `약 ${totalMinutes}분 남음`;
  const hours = Math.floor(totalMinutes / MINUTES_PER_HOUR);
  const minutes = totalMinutes % MINUTES_PER_HOUR;
  return minutes === 0 ? `약 ${hours}시간 남음` : `약 ${hours}시간 ${minutes}분 남음`;
};

export const parseQueueSummary = (payload: unknown): QueueProgress | null => {
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as Record<string, unknown>;
  const counts = body.counts;
  if (typeof counts !== "object" || counts === null) return null;
  const byStatus = counts as Record<string, unknown>;

  const queued = byStatus.queued, running = byStatus.running;
  const active = body.active, finished = body.finished_24h;
  if (!isCount(queued) || !isCount(running) || !isCount(active) || !isCount(finished)) return null;

  const avgSeconds = body.avg_seconds;
  const mean = isDuration(avgSeconds) ? avgSeconds : null;

  if (active === 0) {
    return { queued, running, active, finished, etaText: null,
      headline: "대기 중인 작업이 없습니다." };
  }

  // No progress percentage is reported. The only denominators available are
  // all-time history (retained forever, so a fresh 804-site run would read as
  // nearly complete) and a rolling 24-hour count, which is not the current
  // sweep either -- with sites on daily schedules it pins a bar near 100%
  // permanently. The queue depth and the estimate answer the operator's
  // question without inventing a denominator.
  return {
    queued, running, active, finished,
    etaText: formatEta(mean, active),
    headline: `대기 ${queued.toLocaleString("ko-KR")}건 · 실행 중 ${running.toLocaleString("ko-KR")}건`,
  };
};

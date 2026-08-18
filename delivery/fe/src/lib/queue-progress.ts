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
  percent: number | null;
  etaText: string | null;
  headline: string;
}>;

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

// The worker is a single serial loop -- one site at a time -- so the wait is
// simply the queue depth times how long one site has been taking.
export const formatEta = (seconds: number | null, activeJobs: number): string | null => {
  if (seconds === null || !isCount(seconds) || !isCount(activeJobs) || activeJobs === 0) return null;
  const total = Math.round(seconds * activeJobs);
  if (total < SECONDS_PER_MINUTE) return "1분 미만 남음";
  if (total < SECONDS_PER_HOUR) return `약 ${Math.round(total / SECONDS_PER_MINUTE)}분 남음`;
  const hours = Math.floor(total / SECONDS_PER_HOUR);
  const minutes = Math.round((total % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
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
  const mean = isCount(avgSeconds) ? avgSeconds : null;

  if (active === 0) {
    return { queued, running, active, finished, percent: null, etaText: null,
      headline: "대기 중인 작업이 없습니다." };
  }

  // Against the current sweep, never against all-time history: job records are
  // retained forever, so an all-time denominator would report a fresh 804-site
  // run as already 90% done.
  const denominator = finished + active;
  return {
    queued, running, active, finished,
    percent: denominator === 0 ? null : Math.round((finished / denominator) * 100),
    etaText: formatEta(mean, active),
    headline: `대기 ${queued.toLocaleString("ko-KR")}건 · 실행 중 ${running.toLocaleString("ko-KR")}건`,
  };
};

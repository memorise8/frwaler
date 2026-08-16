import { jobStatusLabel } from "@/lib/job-status";

export const QUEUE_KEYS = ["stale", "recent", "failed"] as const;
export type QueueKey = (typeof QUEUE_KEYS)[number];

export const isQueueKey = (value: string): value is QueueKey =>
  (QUEUE_KEYS as readonly string[]).includes(value);

export const DEFAULT_QUEUE: QueueKey = "stale";

export type QueueRow = Readonly<{
  siteId: string;
  siteName: string;
  country: string;
  documents: number | null;
  note: string;
}>;

// `excluded` counts sites that belong in the queue by its own rule but have
// no crawler to run. /freshness reports on the database, which holds at
// least one site (scienceon-api, 10,447 documents) that no crawler in the
// catalogue produces. Offering it here would hand the operator a row whose
// only possible outcome is a failed job, so it is dropped -- and counted, so
// the screen can say so rather than quietly showing a smaller number than
// the card promised.
export type QueueSelection = Readonly<{ rows: readonly QueueRow[]; excluded: number }>;

export type FreshnessSiteLike = Readonly<{
  site_id: string;
  site_name: string;
  age_days: number | null;
  freshness_bucket: string;
}>;

export type CrawlerLike = Readonly<{
  siteId: string;
  siteName: string;
  country: string;
  status: string;
  category: string;
}>;

export type JobLike = Readonly<{
  id: number;
  site_id: string;
  status: string;
  created_at: string;
}>;

// "over_90_days" and "never" are the two buckets nobody is going to fix by
// waiting. They are deliberately kept together: a site measured at 400 days
// and a site never collected at all are the same job for an operator, and
// splitting them would put the two most urgent rows on separate screens.
const STALE_BUCKETS: ReadonlySet<string> = new Set(["over_90_days", "never"]);

const nameOf = (crawler: CrawlerLike | undefined, fallbackId: string, fallbackName?: string): string =>
  crawler?.siteName || fallbackName || fallbackId;

const indexCrawlers = (crawlers: readonly CrawlerLike[]): Map<string, CrawlerLike> =>
  new Map(crawlers.map((row) => [row.siteId, row]));

export const selectStaleSites = (
  freshness: readonly FreshnessSiteLike[],
  crawlers: readonly CrawlerLike[],
  documentsBySite: ReadonlyMap<string, number>,
): QueueSelection => {
  const byId = indexCrawlers(crawlers);
  const inQueue = freshness.filter((site) => STALE_BUCKETS.has(site.freshness_bucket));
  const runnable = inQueue.filter((site) => byId.has(site.site_id));
  // Never-collected sites sort ahead of everything: age_days is null for them,
  // which is not a small number but an absent measurement, and must not fall
  // through a numeric comparison to the bottom of the list.
  const sorted = [...runnable].sort((a, b) => {
    if (a.age_days === null && b.age_days === null) return a.site_id.localeCompare(b.site_id);
    if (a.age_days === null) return -1;
    if (b.age_days === null) return 1;
    return b.age_days - a.age_days || a.site_id.localeCompare(b.site_id);
  });
  return {
    rows: sorted.map((site) => {
      const crawler = byId.get(site.site_id);
      return {
        siteId: site.site_id,
        siteName: nameOf(crawler, site.site_id, site.site_name),
        country: crawler?.country ?? "",
        documents: documentsBySite.get(site.site_id) ?? null,
        note: site.age_days === null ? "한 번도 수집하지 않음" : `${site.age_days.toLocaleString("ko-KR")}일 전 수집`,
      };
    }),
    excluded: inQueue.length - runnable.length,
  };
};

export const selectFailedCrawlers = (
  crawlers: readonly CrawlerLike[],
  documentsBySite: ReadonlyMap<string, number>,
): QueueSelection => {
  const rows = crawlers
    .filter((row) => row.status === "unhealthy")
    .sort((a, b) => a.siteName.localeCompare(b.siteName, "ko") || a.siteId.localeCompare(b.siteId))
    .map((row) => ({
      siteId: row.siteId,
      siteName: nameOf(row, row.siteId),
      country: row.country,
      documents: documentsBySite.get(row.siteId) ?? null,
      note: row.category || "확인 필요",
    }));
  return { rows, excluded: 0 };
};

// One row per site, not per job: an operator re-running yesterday's work
// wants the site, and a site crawled five times in a row would otherwise
// push everything else off the list. The most recent job supplies the note.
export const selectRecentSites = (
  jobs: readonly JobLike[],
  crawlers: readonly CrawlerLike[],
  documentsBySite: ReadonlyMap<string, number>,
  limit = 12,
): QueueSelection => {
  const byId = indexCrawlers(crawlers);
  const newestFirst = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
  const seen = new Set<string>();
  const rows: QueueRow[] = [];
  let excluded = 0;
  for (const job of newestFirst) {
    if (seen.has(job.site_id)) continue;
    seen.add(job.site_id);
    const crawler = byId.get(job.site_id);
    if (!crawler) {
      excluded += 1;
      continue;
    }
    if (rows.length >= limit) continue;
    rows.push({
      siteId: job.site_id,
      siteName: nameOf(crawler, job.site_id),
      country: crawler.country,
      documents: documentsBySite.get(job.site_id) ?? null,
      note: `최근 작업 #${job.id} · ${jobStatusLabel(job.status)}`,
    });
  }
  return { rows, excluded };
};

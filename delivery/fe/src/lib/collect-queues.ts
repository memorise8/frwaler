import { jobStatusLabel } from "@/lib/job-status";
import { resolveVerificationState, type VerificationRow, type VerificationState } from "@/lib/crawler-verification";

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
  status: string;
  category: string;
  verified: VerificationState;
  note: string;
}>;

// Two different reasons a site can belong to a queue by its own rule and
// still not be offered, kept apart because they need different words:
//
// - noCrawler: /freshness reports on the database, which holds a site no
//   crawler produces (scienceon-api, 10,447 documents). Nothing can run it.
// - unhealthy: the audit already found this crawler broken, and this
//   deployment has not shown otherwise. It is not removed from the console --
//   the 실패·확인 필요 queue exists to re-run exactly these, since several are
//   IP blocks the audit marked "클라이언트 egress에서 재확인 필요" -- but it
//   does not belong in a list of ordinary overdue work, where it costs an
//   operator a full crawl to rediscover what the catalogue already knows.
//   Once a run here has stored a document the exclusion stops applying: the
//   snapshot is then simply wrong about this network, and the site is ordinary
//   overdue work like any other.
export type QueueExclusions = Readonly<{ noCrawler: number; unhealthy: number }>;
export type QueueSelection = Readonly<{ rows: readonly QueueRow[]; excluded: QueueExclusions }>;

const NONE: QueueExclusions = { noCrawler: 0, unhealthy: 0 };

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
  reason: string;
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
  verification: ReadonlyMap<string, VerificationRow> = new Map(),
): QueueSelection => {
  const byId = indexCrawlers(crawlers);
  const inQueue = freshness.filter((site) => STALE_BUCKETS.has(site.freshness_bucket));
  const withCrawler = inQueue.filter((site) => byId.has(site.site_id));
  const runnable = withCrawler.filter((site) =>
    byId.get(site.site_id)!.status !== "unhealthy"
    || resolveVerificationState(verification.get(site.site_id)) === "collected");
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
        status: crawler?.status ?? "",
        category: crawler?.category ?? "",
        verified: resolveVerificationState(verification.get(site.site_id)),
        note: site.age_days === null ? "한 번도 수집하지 않음" : `${site.age_days.toLocaleString("ko-KR")}일 전 수집`,
      };
    }),
    excluded: {
      noCrawler: inQueue.length - withCrawler.length,
      unhealthy: withCrawler.length - runnable.length,
    },
  };
};

export const selectFailedCrawlers = (
  crawlers: readonly CrawlerLike[],
  documentsBySite: ReadonlyMap<string, number>,
  verification: ReadonlyMap<string, VerificationRow> = new Map(),
): QueueSelection => {
  const rows = crawlers
    .filter((row) => row.status === "unhealthy")
    .sort((a, b) => a.siteName.localeCompare(b.siteName, "ko") || a.siteId.localeCompare(b.siteId))
    .map((row) => ({
      siteId: row.siteId,
      siteName: nameOf(row, row.siteId),
      country: row.country,
      documents: documentsBySite.get(row.siteId) ?? null,
      status: row.status,
      category: row.category,
      verified: resolveVerificationState(verification.get(row.siteId)),
      // The badge cell already carries the category, so the note gives the
      // audit's actual sentence -- which is where "코드문제 아님, 클라이언트
      // egress에서 재확인 필요" lives, the difference between a crawler worth
      // retrying here and one that is simply broken.
      note: row.reason || row.category || "확인 필요",
    }));
  return { rows, excluded: NONE };
};

// One row per site, not per job: an operator re-running yesterday's work
// wants the site, and a site crawled five times in a row would otherwise
// push everything else off the list. The most recent job supplies the note.
export const selectRecentSites = (
  jobs: readonly JobLike[],
  crawlers: readonly CrawlerLike[],
  documentsBySite: ReadonlyMap<string, number>,
  limit = 12,
  verification: ReadonlyMap<string, VerificationRow> = new Map(),
): QueueSelection => {
  const byId = indexCrawlers(crawlers);
  const newestFirst = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
  const seen = new Set<string>();
  const rows: QueueRow[] = [];
  let noCrawler = 0;
  for (const job of newestFirst) {
    if (seen.has(job.site_id)) continue;
    seen.add(job.site_id);
    const crawler = byId.get(job.site_id);
    if (!crawler) {
      noCrawler += 1;
      continue;
    }
    if (rows.length >= limit) continue;
    // A crawler the audit calls broken stays in this queue: it is a record
    // of what was actually run, and hiding a run that happened would be a
    // different kind of lie than offering one that cannot work. The status
    // column carries the warning instead.
    rows.push({
      siteId: job.site_id,
      siteName: nameOf(crawler, job.site_id),
      country: crawler.country,
      documents: documentsBySite.get(job.site_id) ?? null,
      status: crawler.status,
      category: crawler.category,
      verified: resolveVerificationState(verification.get(job.site_id)),
      note: `최근 작업 #${job.id} · ${jobStatusLabel(job.status)}`,
    });
  }
  return { rows, excluded: { noCrawler, unhealthy: 0 } };
};

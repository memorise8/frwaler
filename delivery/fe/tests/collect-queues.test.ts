import { describe, expect, it } from "vitest";
import {
  DEFAULT_QUEUE,
  isQueueKey,
  QUEUE_KEYS,
  selectFailedCrawlers,
  selectRecentSites,
  selectStaleSites,
  type CrawlerLike,
  type FreshnessSiteLike,
  type JobLike,
} from "../src/lib/collect-queues";
import type { VerificationRow } from "../src/lib/crawler-verification";

const verified = (siteId: string, bestSaved: number): ReadonlyMap<string, VerificationRow> =>
  new Map([[siteId, {
    site_id: siteId, last_status: "done", last_saved_count: bestSaved, last_error: null,
    last_finished_at: "2026-08-17T12:00:00Z", jobs: 1, best_saved_count: bestSaved,
  }]]);

const crawler = (siteId: string, over: Partial<CrawlerLike> = {}): CrawlerLike => ({
  siteId, siteName: `${siteId} 이름`, country: "독일", status: "healthy", category: "", reason: "", ...over,
});
const NONE = { noCrawler: 0, unhealthy: 0 };

const fresh = (site_id: string, bucket: string, age_days: number | null): FreshnessSiteLike =>
  ({ site_id, site_name: `Custom: ${site_id}`, freshness_bucket: bucket, age_days });

const docs = (entries: readonly (readonly [string, number])[]) => new Map(entries);

describe("queue keys", () => {
  it("accepts only the three defined queues", () => {
    expect(QUEUE_KEYS).toEqual(["stale", "recent", "failed"]);
    expect(isQueueKey("stale")).toBe(true);
    expect(isQueueKey("")).toBe(false);
    expect(isQueueKey("everything")).toBe(false);
  });

  it("defaults to the queue that represents outstanding work", () => {
    expect(DEFAULT_QUEUE).toBe("stale");
  });
});

describe("selectStaleSites", () => {
  const crawlers = [crawler("a"), crawler("b"), crawler("c"), crawler("d")];

  it("keeps only the over-90-day and never-collected buckets", () => {
    const result = selectStaleSites([
      fresh("a", "over_90_days", 120),
      fresh("b", "31_to_90_days", 60),
      fresh("c", "within_7_days", 1),
      fresh("d", "never", null),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["d", "a"]);
  });

  // age_days is null for a never-collected site. That is an absent
  // measurement, not a small number, and must not sort to the bottom.
  it("puts never-collected sites above the oldest measured site", () => {
    const result = selectStaleSites([
      fresh("a", "over_90_days", 400),
      fresh("d", "never", null),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["d", "a"]);
    expect(result.rows[0]!.note).toBe("한 번도 수집하지 않음");
    expect(result.rows[1]!.note).toBe("400일 전 수집");
  });

  it("orders measured sites oldest first", () => {
    const result = selectStaleSites([
      fresh("a", "over_90_days", 95),
      fresh("b", "over_90_days", 300),
      fresh("c", "over_90_days", 150),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["b", "c", "a"]);
  });

  // A site can be in the database with no crawler to run it (scienceon-api).
  // Listing it would offer a row whose only outcome is a failed job.
  it("drops sites that have no crawler and reports how many", () => {
    const result = selectStaleSites([
      fresh("a", "over_90_days", 120),
      fresh("ghost", "over_90_days", 500),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["a"]);
    expect(result.excluded).toEqual({ noCrawler: 1, unhealthy: 0 });
  });

  // A crawler the audit already found broken is not ordinary overdue work:
  // leaving it here costs an operator a full crawl to rediscover what the
  // catalogue already recorded. It stays reachable in the failed queue.
  it("drops crawlers the audit marked unhealthy and counts them separately", () => {
    const result = selectStaleSites([
      fresh("a", "over_90_days", 120),
      fresh("blocked", "never", null),
    ], [...crawlers, crawler("blocked", { status: "unhealthy", category: "IP차단" })], docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["a"]);
    expect(result.excluded).toEqual({ noCrawler: 0, unhealthy: 1 });
  });

  // The delivered verdict is a snapshot from another network that nothing
  // updates. Once a run here has actually stored a document, excluding the
  // site as "known broken" would hide real overdue work behind a stale file.
  it("stops excluding an audit failure once it has collected on this install", () => {
    const catalogue = [...crawlers, crawler("blocked", { status: "unhealthy", category: "IP차단" })];
    const freshness = [fresh("a", "over_90_days", 120), fresh("blocked", "over_90_days", 200)];
    const withoutProof = selectStaleSites(freshness, catalogue, docs([]));
    expect(withoutProof.rows.map((row) => row.siteId)).toEqual(["a"]);

    const withProof = selectStaleSites(freshness, catalogue, docs([]), verified("blocked", 12));
    expect(withProof.rows.map((row) => row.siteId)).toEqual(["blocked", "a"]);
    expect(withProof.excluded).toEqual({ noCrawler: 0, unhealthy: 0 });
    expect(withProof.rows[0]!.verified).toBe("collected");
  });

  it("marks every row with what this install observed", () => {
    const result = selectStaleSites([fresh("a", "over_90_days", 120)], crawlers, docs([]), verified("a", 3));
    expect(result.rows[0]!.verified).toBe("collected");
    expect(selectStaleSites([fresh("b", "over_90_days", 120)], crawlers, docs([])).rows[0]!.verified).toBe("unrun");
  });

  it("carries the audit verdict onto every row it does keep", () => {
    const result = selectStaleSites([fresh("a", "over_90_days", 120)], crawlers, docs([]));
    expect(result.rows[0]).toMatchObject({ status: "healthy", category: "" });
  });

  // The delivery scenario: a customer installs, the migration registers all
  // 804 crawlers as sites, and nothing has ever been collected. Every site
  // arrives in the "never" bucket, so this queue is the first screen's whole
  // work list -- it must not come back empty the way it did before the sites
  // table was seeded.
  it("fills the queue on a cold install where nothing has been collected", () => {
    const catalogue = Array.from({ length: 804 }, (_, index) =>
      crawler(`site-${String(index).padStart(3, "0")}`, index < 32 ? { status: "unhealthy", category: "IP차단" } : {}));
    const freshness = catalogue.map((row) => fresh(row.siteId, "never", null));
    const result = selectStaleSites(freshness, catalogue, docs([]));
    expect(result.rows).toHaveLength(804 - 32);
    expect(result.excluded).toEqual({ noCrawler: 0, unhealthy: 32 });
    expect(new Set(result.rows.map((row) => row.note))).toEqual(new Set(["한 번도 수집하지 않음"]));
  });

  it("prefers the catalogue name over the database's placeholder name", () => {
    const result = selectStaleSites([fresh("a", "never", null)], crawlers, docs([]));
    expect(result.rows[0]!.siteName).toBe("a 이름");
  });

  it("reports a missing document count as null rather than zero", () => {
    const result = selectStaleSites([fresh("a", "never", null), fresh("b", "never", null)], crawlers, docs([["a", 23]]));
    expect(result.rows.map((row) => row.documents)).toEqual([23, null]);
  });
});

describe("selectFailedCrawlers", () => {
  it("selects unhealthy crawlers and labels them with the failure category", () => {
    const result = selectFailedCrawlers([
      crawler("ok"),
      crawler("blocked", { status: "unhealthy", category: "IP 차단", reason: "차단 사유 원문", siteName: "나 사이트" }),
      crawler("broken", { status: "unhealthy", category: "", siteName: "가 사이트" }),
    ], docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["broken", "blocked"]);
    expect(result.rows.map((row) => row.note)).toEqual(["확인 필요", "차단 사유 원문"]);
    expect(result.rows.map((row) => row.category)).toEqual(["", "IP 차단"]);
    expect(result.excluded).toEqual(NONE);
  });
});

describe("selectRecentSites", () => {
  const crawlers = [crawler("a"), crawler("b")];
  const job = (id: number, site_id: string, created_at: string, status = "done"): JobLike =>
    ({ id, site_id, status, created_at });

  it("collapses repeated runs of one site into a single newest row", () => {
    const result = selectRecentSites([
      job(1, "a", "2026-08-01T00:00:00Z"),
      job(3, "a", "2026-08-03T00:00:00Z"),
      job(2, "b", "2026-08-02T00:00:00Z"),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["a", "b"]);
    expect(result.rows[0]!.note).toBe("최근 작업 #3 · 완료");
  });

  it("sorts by recency regardless of the order the backend returned", () => {
    const result = selectRecentSites([
      job(1, "a", "2026-08-01T00:00:00Z"),
      job(2, "b", "2026-08-09T00:00:00Z"),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["b", "a"]);
  });

  it("localises the job status in the note", () => {
    const result = selectRecentSites([job(9, "a", "2026-08-01T00:00:00Z", "running")], crawlers, docs([]));
    expect(result.rows[0]!.note).toBe("최근 작업 #9 · 실행 중");
  });

  it("skips jobs whose site has no crawler and counts them once", () => {
    const result = selectRecentSites([
      job(1, "ghost", "2026-08-01T00:00:00Z"),
      job(2, "ghost", "2026-08-02T00:00:00Z"),
      job(3, "a", "2026-08-03T00:00:00Z"),
    ], crawlers, docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["a"]);
    expect(result.excluded).toEqual({ noCrawler: 1, unhealthy: 0 });
  });

  // Unlike the stale queue, this one keeps a crawler the audit calls broken.
  // It is a record of what was actually run, and hiding a run that happened
  // would be a different kind of lie than offering one that cannot work.
  it("keeps an unhealthy crawler that was recently run, carrying its verdict", () => {
    const result = selectRecentSites(
      [job(1, "bad", "2026-08-01T00:00:00Z")],
      [crawler("bad", { status: "unhealthy", category: "IP차단" })],
      docs([]));
    expect(result.rows.map((row) => row.siteId)).toEqual(["bad"]);
    expect(result.rows[0]).toMatchObject({ status: "unhealthy", category: "IP차단" });
    expect(result.excluded).toEqual(NONE);
  });

  it("caps the list without dropping earlier entries", () => {
    const many = Array.from({ length: 5 }, (_, index) =>
      job(index, `s${index}`, `2026-08-0${index + 1}T00:00:00Z`));
    const catalogue = many.map((item) => crawler(item.site_id));
    const result = selectRecentSites(many, catalogue, docs([]), 2);
    expect(result.rows.map((row) => row.siteId)).toEqual(["s4", "s3"]);
  });

  it("returns nothing when no job has ever run", () => {
    expect(selectRecentSites([], crawlers, docs([]))).toEqual({ rows: [], excluded: NONE });
  });
});

import { describe, expect, it } from "vitest";
import { RUN_PICKER_LIMIT, searchCrawlersToRun } from "../src/lib/crawler-search";

const rows = [
  { siteId: "bfr-bund-de-en", siteName: "독일 연방위해평가원 (영문)" },
  { siteId: "bfr-bund-de", siteName: "독일 연방위해평가원" },
  { siteId: "anses-fr", siteName: "프랑스 식품환경노동위생안전청" },
  { siteId: "efsa-europa-eu", siteName: "European Food Safety Authority" },
];

describe("searchCrawlersToRun", () => {
  // The picker starts a crawl against a third-party site. An empty query must
  // never fall back to "here is everything", which would put 800 live targets
  // one misclick away.
  it("returns nothing for an empty or whitespace query", () => {
    expect(searchCrawlersToRun(rows, "")).toEqual({ matches: [], total: 0, truncated: false });
    expect(searchCrawlersToRun(rows, "   ")).toEqual({ matches: [], total: 0, truncated: false });
  });

  it("matches on site id and on display name", () => {
    expect(searchCrawlersToRun(rows, "bfr").matches.map((row) => row.siteId)).toEqual(["bfr-bund-de", "bfr-bund-de-en"]);
    expect(searchCrawlersToRun(rows, "프랑스").matches.map((row) => row.siteId)).toEqual(["anses-fr"]);
  });

  it("is case-insensitive", () => {
    expect(searchCrawlersToRun(rows, "EFSA").matches.map((row) => row.siteId)).toEqual(["efsa-europa-eu"]);
    expect(searchCrawlersToRun(rows, "european food").matches.map((row) => row.siteId)).toEqual(["efsa-europa-eu"]);
  });

  it("puts an exact site id first, ahead of a longer id that merely starts with it", () => {
    expect(searchCrawlersToRun(rows, "bfr-bund-de").matches.map((row) => row.siteId)).toEqual(["bfr-bund-de", "bfr-bund-de-en"]);
  });

  it("ranks a prefix match ahead of a match found in the middle of a name", () => {
    const ranked = searchCrawlersToRun([
      { siteId: "z-site", siteName: "국립 안전 연구원" },
      { siteId: "a-site", siteName: "연구원 통합 포털" },
    ], "연구원");
    expect(ranked.matches.map((row) => row.siteId)).toEqual(["a-site", "z-site"]);
  });

  it("reports the full match count while capping the rendered rows", () => {
    const many = Array.from({ length: RUN_PICKER_LIMIT + 5 }, (_, index) => ({
      siteId: `site-${String(index).padStart(3, "0")}`,
      siteName: `사이트 ${index}`,
    }));
    const result = searchCrawlersToRun(many, "site-");
    expect(result.total).toBe(RUN_PICKER_LIMIT + 5);
    expect(result.matches).toHaveLength(RUN_PICKER_LIMIT);
    expect(result.truncated).toBe(true);
  });

  it("does not flag truncation when everything fits", () => {
    const result = searchCrawlersToRun(rows, "e");
    expect(result.truncated).toBe(false);
    expect(result.matches).toHaveLength(result.total);
  });

  it("leaves the caller's array untouched", () => {
    const input = [...rows];
    searchCrawlersToRun(input, "bfr");
    expect(input).toEqual(rows);
  });
});

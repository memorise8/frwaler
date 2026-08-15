import { describe, expect, it } from "vitest";
import { crawlerDocumentsHref, formatEvidenceDate, summarizeCrawlerEvidence } from "../src/lib/crawler-evidence";
import type { DocumentCatalogue } from "../src/lib/document-catalogue";

const baseData: DocumentCatalogue = {
  items: [],
  pagination: { page: 1, page_size: 5, total: 0, pages: 0 },
  facets: { countries: [], doc_types: [], sites: [], languages: [] },
  query: { q: null, sort: "collected_desc" },
  measured_at: "2026-08-15T15:26:11.242769+00:00",
};

describe("summarizeCrawlerEvidence", () => {
  it("reports unavailable when the fetch failed outright", () => {
    expect(summarizeCrawlerEvidence({ ok: false, kind: "unavailable" })).toEqual({ kind: "unavailable" });
  });

  it("reports unavailable when the request was rejected as invalid", () => {
    // The evidence query is built by this app, not by operator input, so a
    // 422 is as unexpected as a network failure -- both mean "could not
    // check", not "checked, found nothing".
    expect(summarizeCrawlerEvidence({ ok: false, kind: "invalid" })).toEqual({ kind: "unavailable" });
  });

  it("distinguishes zero documents from a failed check", () => {
    const result = { ok: true as const, data: baseData };
    expect(summarizeCrawlerEvidence(result)).toEqual({ kind: "empty", measuredAt: baseData.measured_at });
  });

  it("carries total, items, and facets through when documents exist", () => {
    const data: DocumentCatalogue = {
      ...baseData,
      pagination: { page: 1, page_size: 5, total: 23, pages: 5 },
      items: [{
        seq_id: 436371, site_id: "bfr-bund-de-en", site_name: "Custom: bfr-bund-de-en", country: "독일",
        doc_type: "보고서", title: "The BfR in brief", published_date: "2026-05-29",
        collected_at: "2026-07-18T08:26:12+00:00", authors: null, publisher: "BfR", journal: null,
        lang: "en", has_pdf: true, has_text: true, has_translation: true, meta_url: "https://example.org",
      }],
      facets: {
        countries: [{ value: "독일", count: 23 }],
        doc_types: [{ value: "보고서", count: 23 }],
        sites: [{ value: "bfr-bund-de-en", count: 23 }],
        languages: [{ value: "en", count: 15 }, { value: "de", count: 8 }],
      },
    };
    expect(summarizeCrawlerEvidence({ ok: true, data })).toEqual({
      kind: "found",
      total: 23,
      measuredAt: data.measured_at,
      items: data.items,
      docTypes: data.facets.doc_types,
      languages: data.facets.languages,
    });
  });
});

describe("formatEvidenceDate", () => {
  it("labels a missing date explicitly rather than leaving it blank", () => {
    expect(formatEvidenceDate(null)).toBe("날짜 미상");
  });

  it("formats a date-only value", () => {
    expect(formatEvidenceDate("2026-05-29")).toBe(new Date("2026-05-29T00:00:00Z").toLocaleDateString("ko-KR"));
  });

  it("formats a full timestamp", () => {
    expect(formatEvidenceDate("2026-07-18T08:26:12+00:00")).toBe(new Date("2026-07-18T08:26:12+00:00").toLocaleDateString("ko-KR"));
  });

  it("falls back to the raw value when it cannot be parsed", () => {
    expect(formatEvidenceDate("not-a-date")).toBe("not-a-date");
  });
});

describe("crawlerDocumentsHref", () => {
  it("scopes the link to the given site", () => {
    expect(crawlerDocumentsHref("bfr-bund-de-en")).toBe("/documents?site_id=bfr-bund-de-en");
  });

  it("adds an extra filter when given one", () => {
    expect(crawlerDocumentsHref("bfr-bund-de-en", { key: "doc_type", value: "보고서" }))
      .toBe(`/documents?site_id=bfr-bund-de-en&doc_type=${encodeURIComponent("보고서")}`);
  });

  it("encodes site ids that need it", () => {
    expect(crawlerDocumentsHref("a b&c")).toBe("/documents?site_id=a+b%26c");
  });
});

import { describe, expect, it } from "vitest";
import {
  Q_REQUIRES_SITE_NOTICE,
  RELEVANCE_SORT_LABEL,
  buildCatalogueQuery,
  defaultSortLabel,
  cursorHref,
  formatTotal,
  hasAdvancedFilters,
  invalidCatalogueMessage,
  searchDisabledReason,
} from "../src/lib/document-paging";
import { facetCountLabel } from "../src/lib/facet-options";
import { summarizeCrawlerEvidence } from "../src/lib/crawler-evidence";

describe("buildCatalogueQuery", () => {
  it("sends q together with a site and keeps the cursor", () => {
    const { apiParams, qNotice, selected } = buildCatalogueQuery({ q: " climate ", site_id: "scienceon-api", cursor: "abc" });
    expect(qNotice).toBeNull();
    expect(apiParams.get("q")).toBe("climate");
    expect(apiParams.get("cursor")).toBe("abc");
    expect(apiParams.get("page_size")).toBe("20");
    expect(selected.q).toBe("climate");
  });

  it("drops q without a site and raises the notice, keeping it visible in the form", () => {
    const { apiParams, qNotice, selected } = buildCatalogueQuery({ q: "climate", country: "한국" });
    expect(qNotice).toBe(Q_REQUIRES_SITE_NOTICE);
    expect(qNotice).toBe("검색하려면 먼저 사이트를 고르세요");
    expect(apiParams.has("q")).toBe(false);
    expect(apiParams.get("country")).toBe("한국");
    expect(selected.q).toBe("climate");
  });

  it("drops relevance sort when q cannot be sent", () => {
    expect(buildCatalogueQuery({ q: "climate", sort: "relevance" }).apiParams.has("sort")).toBe(false);
    expect(buildCatalogueQuery({ sort: "relevance", site_id: "s" }).apiParams.has("sort")).toBe(false);
    expect(buildCatalogueQuery({ q: "x", sort: "relevance", site_id: "s" }).apiParams.get("sort")).toBe("relevance");
  });

  it("keeps page bookmarks up to page 5 only, and never alongside a cursor", () => {
    expect(buildCatalogueQuery({ page: "3" }).apiParams.get("page")).toBe("3");
    expect(buildCatalogueQuery({ page: "5" }).apiParams.get("page")).toBe("5");
    expect(buildCatalogueQuery({ page: "1" }).apiParams.has("page")).toBe(false);
    expect(buildCatalogueQuery({ page: "2.5" }).apiParams.has("page")).toBe(false);
    expect(buildCatalogueQuery({ page: "3", cursor: "c" }).apiParams.has("page")).toBe(false);
  });

  it("ignores deep page numbers and unknown parameters", () => {
    const { apiParams } = buildCatalogueQuery({ page: "9", evil: "1", site_id: ["a", "b"] });
    expect(apiParams.has("page")).toBe(false);
    expect(apiParams.has("evil")).toBe(false);
    expect(apiParams.get("site_id")).toBe("a");
  });
});

describe("sort labels", () => {
  it("names the default sort the BE applies with and without a search term", () => {
    expect(defaultSortLabel("")).toBe("기본 정렬 (최신 수집일)");
    expect(defaultSortLabel("  ")).toBe("기본 정렬 (최신 수집일)");
    expect(defaultSortLabel("연구")).toBe("기본 정렬 (최신 발행일)");
    expect(RELEVANCE_SORT_LABEL).toContain("큰 사이트에서는 느릴 수 있음");
  });

  it("does not add a sort when none was chosen, so the BE default applies", () => {
    expect(buildCatalogueQuery({ q: "연구", site_id: "s" }).apiParams.has("sort")).toBe(false);
    expect(buildCatalogueQuery({ q: "연구", site_id: "s", sort: "collected_desc" }).apiParams.get("sort")).toBe("collected_desc");
  });
});

describe("cursorHref", () => {
  const params = new URLSearchParams({ site_id: "s", sort: "published_desc", page_size: "20", cursor: "old", page: "2" });

  it("replaces the cursor and keeps filters", () => {
    const href = cursorHref(params, "next-token");
    const query = new URLSearchParams(href.split("?")[1]);
    expect(href.startsWith("/documents?")).toBe(true);
    expect(query.get("cursor")).toBe("next-token");
    expect(query.get("site_id")).toBe("s");
    expect(query.get("sort")).toBe("published_desc");
    expect(query.has("page_size")).toBe(false);
    expect(query.has("page")).toBe(false);
  });

  it("returns to the first page with a null cursor", () => {
    expect(cursorHref(params, null)).toBe("/documents?site_id=s&sort=published_desc");
    expect(cursorHref(new URLSearchParams({ page_size: "20" }), null)).toBe("/documents");
  });
});

describe("formatTotal", () => {
  it("formats exact and capped totals", () => {
    expect(formatTotal(1234, true)).toBe("1,234건");
    expect(formatTotal(1234, undefined)).toBe("1,234건");
    expect(formatTotal(10000, false)).toBe("10,000건 이상");
    expect(formatTotal(10000, false, "capped_count")).toBe("10,000건 이상");
    expect(formatTotal(320000, false, "estimate")).toBe("약 320,000건");
    expect(formatTotal(5, true, "estimate")).toBe("5건");
  });
});

describe("invalidCatalogueMessage", () => {
  it("explains known 422 reasons", () => {
    expect(invalidCatalogueMessage("q_requires_site")).toBe(Q_REQUIRES_SITE_NOTICE);
    expect(invalidCatalogueMessage("invalid_cursor")).toContain("첫 페이지");
    expect(invalidCatalogueMessage(undefined)).toContain("필터 값");
  });
});

describe("facet counts from the new response", () => {
  it("labels counted, uncounted and retained options", () => {
    expect(facetCountLabel({ value: "en", count: 3400000 })).toBe(" (3,400,000)");
    expect(facetCountLabel({ value: "en", count: null })).toBe("");
    expect(facetCountLabel({ value: "en", count: -1 })).toBe(" (선택값 유지)");
  });

  it("crawler evidence ignores uncounted facets", () => {
    const evidence = summarizeCrawlerEvidence({
      ok: true,
      data: {
        items: [],
        pagination: { page: 1, page_size: 5, total: 3, total_exact: true, pages: 1, next_cursor: null },
        facets: {
          countries: [], sites: [],
          doc_types: [{ value: "보고서", count: null }],
          languages: [{ value: "en", count: 3 }],
        },
        query: { q: null, sort: "collected_desc" },
        measured_at: "2026-10-20T00:00:00Z",
      },
    });
    expect(evidence.kind).toBe("found");
    if (evidence.kind === "found") {
      expect(evidence.docTypes).toEqual([]);
      expect(evidence.languages).toEqual([{ value: "en", count: 3 }]);
    }
  });
});

describe("search box state", () => {
  it("is disabled with a reason until a site is chosen", () => {
    expect(searchDisabledReason("")).toBe("검색하려면 먼저 사이트를 고르세요");
    expect(searchDisabledReason("  ")).toBe(Q_REQUIRES_SITE_NOTICE);
    expect(searchDisabledReason("prism")).toBeNull();
  });

  it("opens the detailed filters only when one of them is set", () => {
    expect(hasAdvancedFilters(buildCatalogueQuery({ q: "x", site_id: "s" }).selected)).toBe(false);
    expect(hasAdvancedFilters(buildCatalogueQuery({ has_pdf: "true" }).selected)).toBe(true);
    expect(hasAdvancedFilters(buildCatalogueQuery({ sort: "seq_desc" }).selected)).toBe(true);
  });
});

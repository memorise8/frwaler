// Query/paging rules for the /documents page, kept pure so they can be tested
// without rendering the server component.
//
// The BE pages with opaque cursors (next_cursor / prev_cursor) instead of page
// numbers, accepts free-text q only together with site_id (422
// "q_requires_site" otherwise), and reports totals beyond 10,000 as inexact.

export const Q_REQUIRES_SITE_NOTICE = "검색하려면 먼저 사이트를 고르세요";

// The search box stays disabled (with this reason) until a site is chosen.
export const searchDisabledReason = (siteId: string): string | null =>
  siteId.trim() ? null : Q_REQUIRES_SITE_NOTICE;

// Matches the BE's MAX_OFFSET_PAGE.
export const MAX_OFFSET_PAGE = 5;

export const CATALOGUE_PARAMS = [
  "q", "site_id", "country", "doc_type", "lang", "published_from", "published_to",
  "collected_from", "collected_to", "has_pdf", "has_text", "sort", "cursor",
] as const;

export type CatalogueParam = (typeof CATALOGUE_PARAMS)[number];

// Filters kept in the collapsed "상세 조건" panel; it opens when any is set.
export const ADVANCED_PARAMS = [
  "country", "doc_type", "lang", "published_from", "published_to",
  "collected_from", "collected_to", "has_pdf", "has_text", "sort",
] as const satisfies readonly CatalogueParam[];

export const hasAdvancedFilters = (selected: Readonly<Record<CatalogueParam, string>>): boolean =>
  ADVANCED_PARAMS.some((key) => selected[key] !== "");
type Source = Record<string, string | string[] | undefined>;

const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";

export type CatalogueQuery = Readonly<{
  // Parameters sent to the BE.
  apiParams: URLSearchParams;
  // What the form shows (q stays visible even when it was not sent).
  selected: Readonly<Record<CatalogueParam, string>>;
  // Set when q was typed without a site: q is dropped from the BE request.
  qNotice: string | null;
}>;

export const buildCatalogueQuery = (source: Source, pageSize = 20): CatalogueQuery => {
  const selected = Object.fromEntries(CATALOGUE_PARAMS.map((key) => [key, one(source[key]).trim()])) as Record<CatalogueParam, string>;
  const apiParams = new URLSearchParams();
  const qWithoutSite = selected.q !== "" && selected.site_id === "";
  // Old ?page=N bookmarks keep working for the pages the BE still serves by
  // offset; deeper pages need a cursor, so they start over at page 1.
  const page = Number(one(source.page).trim());
  if (!selected.cursor && Number.isInteger(page) && page > 1 && page <= MAX_OFFSET_PAGE) apiParams.set("page", String(page));
  for (const key of CATALOGUE_PARAMS) {
    const value = selected[key];
    if (!value) continue;
    if (qWithoutSite && key === "q") continue;
    // Relevance ranking needs q (and therefore a site); fall back to the default sort.
    if (key === "sort" && value === "relevance" && (qWithoutSite || !selected.q)) continue;
    apiParams.set(key, value);
  }
  apiParams.set("page_size", String(pageSize));
  return { apiParams, selected, qNotice: qWithoutSite ? Q_REQUIRES_SITE_NOTICE : null };
};

// With a search term the BE defaults to published_desc (relevance can be slow
// for common terms in very large sites, so it is opt-in).
export const RELEVANCE_SORT_LABEL = "관련도순 (큰 사이트에서는 느릴 수 있음)";
export const defaultSortLabel = (q: string): string =>
  q.trim() ? "기본 정렬 (최신 발행일)" : "기본 정렬 (최신 수집일)";

// Link to the page a cursor points at; filters are kept, page_size and any
// previous cursor are replaced. A null cursor means "first page".
export const cursorHref = (apiParams: URLSearchParams, cursor: string | null): string => {
  const next = new URLSearchParams(apiParams);
  next.delete("page_size");
  next.delete("page");
  if (cursor) next.set("cursor", cursor); else next.delete("cursor");
  return `/documents${next.size ? `?${next}` : ""}`;
};

// Totals are exact when they come from the counts table or a count under the
// cap. Otherwise the BE either stopped counting at the cap ("at least this
// many") or only has the planner's estimate ("about this many").
export const formatTotal = (total: number, exact: boolean | undefined, source?: string): string => {
  const value = total.toLocaleString("ko-KR");
  if (exact !== false) return `${value}건`;
  return source === "estimate" ? `약 ${value}건` : `${value}건 이상`;
};

const INVALID_MESSAGES: Readonly<Record<string, string>> = {
  q_requires_site: Q_REQUIRES_SITE_NOTICE,
  page_requires_cursor: "앞쪽 몇 페이지 이후는 '다음' 버튼으로 이동해 주세요.",
  invalid_cursor: "페이지 위치 정보가 올바르지 않습니다. 첫 페이지부터 다시 탐색해 주세요.",
  cursor_filter_mismatch: "검색 조건이 바뀌어 첫 페이지부터 다시 보여 드려야 합니다.",
  cursor_sort_mismatch: "정렬이 바뀌어 첫 페이지부터 다시 보여 드려야 합니다.",
};

export const invalidCatalogueMessage = (reason: string | undefined): string =>
  (reason && INVALID_MESSAGES[reason]) || "날짜 범위나 필터 값이 올바른지 확인한 뒤 다시 검색해 주세요.";

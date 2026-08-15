import type { CatalogueResult, DocumentItem } from "./document-catalogue";

type Facet = Readonly<{ value: string; count: number }>;

export type CrawlerEvidence =
  | Readonly<{ kind: "unavailable" }>
  | Readonly<{ kind: "empty"; measuredAt: string }>
  | Readonly<{
      kind: "found";
      total: number;
      measuredAt: string;
      items: readonly DocumentItem[];
      docTypes: readonly Facet[];
      languages: readonly Facet[];
    }>;

// The crawler detail page asks "what has this crawler actually collected,
// right now" by querying the same live /documents endpoint the document
// catalogue page uses. That endpoint only tells you two things directly: an
// ok flag (did the request even complete) and, inside a successful payload,
// a total (did it find anything). The operator-visible question has three
// answers -- "can't check right now", "checked, found nothing", "checked,
// here's what's there" -- and getting the first two confused is exactly the
// failure mode called out for this page: an empty table with no explanation
// reads the same as a broken connection. Centralizing the mapping here
// means the page component only ever renders one of three known shapes.
export const summarizeCrawlerEvidence = (result: CatalogueResult): CrawlerEvidence => {
  if (!result.ok) return { kind: "unavailable" };
  const { data } = result;
  if (data.pagination.total === 0) return { kind: "empty", measuredAt: data.measured_at };
  return {
    kind: "found",
    total: data.pagination.total,
    measuredAt: data.measured_at,
    items: data.items,
    docTypes: data.facets.doc_types,
    languages: data.facets.languages,
  };
};

// Mirrors the date formatting the document catalogue page already applies
// to published/collected dates (10-char values are date-only and need a
// synthetic UTC time before Date can parse them consistently; anything that
// still fails to parse is shown verbatim rather than as "Invalid Date").
export const formatEvidenceDate = (value: string | null): string => {
  if (!value) return "날짜 미상";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString("ko-KR");
};

// The evidence section's outbound links (the doc-type/language mix tiles,
// and the "전체 문서 목록" link) all point back into /documents scoped to
// this site, optionally narrowed by one more field. Centralizing the query
// string construction keeps every one of those links using the same
// encoding rules instead of ad hoc template strings scattered through the
// page component.
export const crawlerDocumentsHref = (siteId: string, extra?: Readonly<{ key: string; value: string }>): string => {
  const params = new URLSearchParams({ site_id: siteId });
  if (extra) params.set(extra.key, extra.value);
  return `/documents?${params}`;
};

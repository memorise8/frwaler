// /collected groups documents by country and material type. Sites without a
// value land in "기타", which on its own says nothing; this keeps the reason
// (not in the crawler list vs. in the list but unclassified) and the largest
// sites in each part so the page can show what "기타" actually contains.

export const OTHER = "기타";

type SiteCount = Readonly<{ key: string; documents: number }>;
type CrawlerInfo = Readonly<{ siteName: string; country: string; docType: string }>;

export type OtherPart = Readonly<{
  reason: "uncatalogued" | "unclassified";
  label: string;
  documents: number;
  sites: number;
  topSites: readonly Readonly<{ siteId: string; name: string; documents: number }>[];
}>;

export type Grouped = Readonly<{
  rows: readonly (readonly [string, number])[];
  otherParts: readonly OtherPart[];
}>;

const PART_LABEL = {
  uncatalogued: "수집기 목록에 없는 사이트",
  country: "수집기는 있으나 국가가 정해지지 않은 사이트",
  docType: "수집기는 있으나 자료 유형이 정해지지 않은 사이트",
} as const;

export const groupDocuments = (
  bySite: readonly SiteCount[],
  crawlers: ReadonlyMap<string, CrawlerInfo>,
  field: "country" | "docType",
  topN = 5,
): Grouped => {
  const totals = new Map<string, number>();
  const parts = new Map<OtherPart["reason"], { documents: number; sites: { siteId: string; name: string; documents: number }[] }>();
  for (const site of bySite) {
    const crawler = crawlers.get(site.key);
    const value = crawler?.[field] || OTHER;
    totals.set(value, (totals.get(value) ?? 0) + site.documents);
    if (value !== OTHER) continue;
    const reason = crawler ? "unclassified" : "uncatalogued";
    const part = parts.get(reason) ?? { documents: 0, sites: [] };
    part.documents += site.documents;
    part.sites.push({ siteId: site.key, name: crawler?.siteName || site.key, documents: site.documents });
    parts.set(reason, part);
  }
  const otherParts = [...parts].map(([reason, part]): OtherPart => ({
    reason,
    label: reason === "uncatalogued" ? PART_LABEL.uncatalogued : PART_LABEL[field],
    documents: part.documents,
    sites: part.sites.length,
    topSites: [...part.sites].sort((a, b) => b.documents - a.documents).slice(0, topN),
  })).sort((a, b) => b.documents - a.documents);
  return { rows: [...totals].sort((a, b) => b[1] - a[1]), otherParts };
};

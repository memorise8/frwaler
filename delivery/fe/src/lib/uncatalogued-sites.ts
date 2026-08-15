export type SiteDocumentCount = Readonly<{ key: string; documents: number }>;

export type UncataloguedSite = Readonly<{ siteId: string; documents: number }>;

// The database (`databaseStats.by_site`) and the crawler catalogue
// (`getCrawlerHealth()`) count two different populations: data sources vs.
// crawlers that collect them. A site can hold documents in the database
// without any crawler entry — an API integration rather than a crawl, for
// example — and such sites are otherwise invisible to every crawler-facing
// view (catalogue table, schedule picker, fleet health counts). This finds
// them so the home page can say so instead of silently folding them into a
// "기타" bucket.
//
// Direction matters: a catalogue entry with no matching database row is not
// reported here. That is a crawler that has not collected anything yet, not
// an uncatalogued data source, and is out of scope for this function.
export const findUncataloguedSites = (
  bySite: readonly SiteDocumentCount[],
  cataloguedSiteIds: readonly string[],
): readonly UncataloguedSite[] => {
  const known = new Set(cataloguedSiteIds);
  return bySite
    .filter((site) => !known.has(site.key))
    .map((site) => ({ siteId: site.key, documents: site.documents }));
};

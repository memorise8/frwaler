export type SearchableCrawler = Readonly<{ siteId: string; siteName: string }>;

export type CrawlerSearchResult<T> = Readonly<{
  matches: readonly T[];
  total: number;
  truncated: boolean;
}>;

export const RUN_PICKER_LIMIT = 30;

// The run picker searches identity only -- site id and display name. The
// catalogue on /crawlers deliberately folds the failure reason into its
// haystack, which is right for "show me every crawler blocked by an IP
// filter" but wrong here: an operator choosing what to run types the name
// of a site, and a match inside diagnosis prose would put crawlers with
// unrelated names into the list of things about to hit a third-party
// server.
const rank = (row: SearchableCrawler, needle: string): number => {
  const id = row.siteId.toLocaleLowerCase("ko-KR");
  const name = row.siteName.toLocaleLowerCase("ko-KR");
  if (id === needle) return 0;
  if (id.startsWith(needle)) return 1;
  if (name.startsWith(needle)) return 2;
  return 3;
};

// An empty query returns nothing rather than everything: this list feeds a
// panel that starts a real crawl, so the default state is "you have not
// chosen yet", never all 800 crawlers waiting for a misclick.
export const searchCrawlersToRun = <T extends SearchableCrawler>(
  rows: readonly T[],
  query: string,
  limit: number = RUN_PICKER_LIMIT,
): CrawlerSearchResult<T> => {
  const needle = query.trim().toLocaleLowerCase("ko-KR");
  if (!needle) return { matches: [], total: 0, truncated: false };
  const matched = rows.filter((row) =>
    row.siteId.toLocaleLowerCase("ko-KR").includes(needle)
    || row.siteName.toLocaleLowerCase("ko-KR").includes(needle));
  const ranked = [...matched].sort((a, b) =>
    rank(a, needle) - rank(b, needle)
    || a.siteName.localeCompare(b.siteName, "ko")
    || a.siteId.localeCompare(b.siteId));
  return { matches: ranked.slice(0, limit), total: ranked.length, truncated: ranked.length > limit };
};

export type NavItem = Readonly<{ href: string; label: string; match: readonly string[] }>;

// Ordered by the operator's actual sequence of work, not by how the data
// happens to be organised: start a collection, watch the crawlers doing it,
// then check what came out of it. Scheduling is a periodic setup task rather
// than something touched on every visit, so it sits after the daily screens.
export const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "수집", match: ["/"] },
  { href: "/crawlers", label: "크롤러 상태", match: ["/crawlers"] },
  { href: "/backfill", label: "백필", match: ["/backfill"] },
  { href: "/collected", label: "수집 상태", match: ["/collected"] },
  { href: "/documents", label: "문서 탐색", match: ["/documents"] },
  { href: "/schedules", label: "수집 예약", match: ["/schedules"] },
  { href: "/translations", label: "번역 작업", match: ["/translations"] },
];

export const isActiveNav = (pathname: string, matches: readonly string[]): boolean =>
  matches.some((prefix) => prefix === "/" ? pathname === "/" : pathname === prefix || pathname.startsWith(`${prefix}/`));

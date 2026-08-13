export type NavItem = Readonly<{ href: string; label: string; match: readonly string[] }>;

export const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "크롤러 상태", match: ["/", "/crawlers"] },
  { href: "/schedules", label: "수집 예약", match: ["/schedules"] },
  { href: "/documents", label: "문서 탐색", match: ["/documents"] },
  { href: "/translations", label: "번역 작업", match: ["/translations"] },
];

export const isActiveNav = (pathname: string, matches: readonly string[]): boolean =>
  matches.some((prefix) => prefix === "/" ? pathname === "/" : pathname === prefix || pathname.startsWith(`${prefix}/`));

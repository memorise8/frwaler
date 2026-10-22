// `locked` marks a route that sits behind the separate Basic-auth prompt, so
// the menu can warn before an operator clicks into a password dialog.
export type NavItem = Readonly<{ href: string; label: string; match: readonly string[]; locked?: boolean }>;
export type NavGroup = Readonly<{ label: string; items: readonly NavItem[] }>;

// Grouped by what the operator is doing: starting collection, watching it,
// reading what came out. Each label is meant to equal the page's <h1>.
// "/" (the job dashboard) asks for a separate password, so it is kept last
// and labelled as such instead of being the first, unexplained click.
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: "수집",
    items: [
      { href: "/site-collect", label: "사이트 수집", match: ["/site-collect"] },
      { href: "/schedules", label: "수집 예약", match: ["/schedules"] },
      { href: "/backfill", label: "백필 진행", match: ["/backfill"] },
    ],
  },
  {
    label: "상태",
    items: [
      { href: "/crawlers", label: "수집기 상태", match: ["/crawlers"] },
      { href: "/health", label: "건강 점검", match: ["/health"] },
      { href: "/collected", label: "수집 상태", match: ["/collected"] },
    ],
  },
  {
    label: "결과",
    items: [
      { href: "/documents", label: "문서 탐색", match: ["/documents"] },
      { href: "/translations", label: "번역 작업", match: ["/translations"] },
    ],
  },
  {
    label: "기타",
    items: [{ href: "/", label: "작업 대시보드 (암호 필요)", match: ["/"], locked: true }],
  },
];

export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);

// Where the logo goes: the first screen an operator works in, never the
// password-protected "/".
export const HOME_HREF = "/site-collect";

export const isActiveNav = (pathname: string, matches: readonly string[]): boolean =>
  matches.some((prefix) => prefix === "/" ? pathname === "/" : pathname === prefix || pathname.startsWith(`${prefix}/`));

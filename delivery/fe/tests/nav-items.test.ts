import { describe, expect, it } from "vitest";
import { HOME_HREF, isActiveNav, NAV_GROUPS, NAV_ITEMS } from "../src/lib/nav-items";

describe("isActiveNav", () => {
  it("matches the root route only exactly", () => {
    expect(isActiveNav("/", ["/"])).toBe(true);
    expect(isActiveNav("/documents", ["/"])).toBe(false);
    expect(isActiveNav("/crawlers", ["/"])).toBe(false);
  });

  it("matches a section and its nested routes", () => {
    expect(isActiveNav("/crawlers/abc", ["/crawlers"])).toBe(true);
    expect(isActiveNav("/documents", ["/documents"])).toBe(true);
    expect(isActiveNav("/documents/12", ["/documents"])).toBe(true);
  });

  it("does not match a route that merely starts with the same characters", () => {
    expect(isActiveNav("/documentsxyz", ["/documents"])).toBe(false);
    expect(isActiveNav("/schedules-archive", ["/schedules"])).toBe(false);
    expect(isActiveNav("/collectedxyz", ["/collected"])).toBe(false);
  });
});

describe("NAV_GROUPS / NAV_ITEMS", () => {
  it("groups the menu as 수집 · 상태 · 결과 · 기타 in operator order", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual(["수집", "상태", "결과", "기타"]);
    expect(NAV_GROUPS.map((group) => group.items.map((item) => item.label))).toEqual([
      ["사이트 수집", "수집 예약", "백필 진행"],
      ["수집기 상태", "건강 점검", "수집 상태"],
      ["문서 탐색", "번역 작업"],
      ["작업 대시보드 (암호 필요)"],
    ]);
    expect(NAV_ITEMS.map((item) => item.href)).toEqual(["/site-collect", "/schedules", "/backfill", "/crawlers", "/health", "/collected", "/documents", "/translations", "/"]);
  });

  // "/" sits behind a separate Basic-auth prompt: neither the logo nor an
  // unmarked menu item may lead there.
  it("marks the password-protected dashboard and keeps the logo off it", () => {
    expect(HOME_HREF).toBe("/site-collect");
    const locked = NAV_ITEMS.filter((item) => item.locked);
    expect(locked.map((item) => item.href)).toEqual(["/"]);
    expect(locked[0]!.label).toContain("암호 필요");
  });

  it("uses 수집기, never 크롤러, in menu labels", () => {
    for (const item of NAV_ITEMS) expect(item.label).not.toContain("크롤러");
  });

  it("activates exactly one section for every console route", () => {
    const routes = ["/", "/site-collect", "/crawlers", "/crawlers/site-1", "/health", "/backfill", "/collected", "/schedules", "/documents", "/documents/9", "/translations"];
    for (const route of routes) {
      const matched = NAV_ITEMS.filter((item) => isActiveNav(route, item.match));
      expect(matched, route).toHaveLength(1);
    }
  });

  it("keeps the crawler detail page under 수집기 상태", () => {
    const matched = NAV_ITEMS.filter((item) => isActiveNav("/crawlers/bfr-bund-de-en", item.match));
    expect(matched.map((item) => item.label)).toEqual(["수집기 상태"]);
  });
});

import { describe, expect, it } from "vitest";
import { isActiveNav, NAV_ITEMS } from "../src/lib/nav-items";

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

describe("NAV_ITEMS", () => {
  // Running a collection is the first thing an operator does, so it owns the
  // landing route; crawler health and collected data follow in the order the
  // work actually happens.
  it("leads with collection and orders the sections by operator workflow", () => {
    expect(NAV_ITEMS.map((item) => item.href)).toEqual(["/", "/crawlers", "/collected", "/documents", "/schedules", "/translations"]);
    expect(NAV_ITEMS.map((item) => item.label)).toEqual(["수집", "크롤러 상태", "수집 상태", "문서 탐색", "수집 예약", "번역 작업"]);
  });

  it("activates exactly one section for every console route", () => {
    const routes = ["/", "/crawlers", "/crawlers/site-1", "/collected", "/schedules", "/documents", "/documents/9", "/translations"];
    for (const route of routes) {
      const matched = NAV_ITEMS.filter((item) => isActiveNav(route, item.match));
      expect(matched, route).toHaveLength(1);
    }
  });

  // The crawler detail page is reached from the run picker on / as well as
  // from the catalogue, and it must not light up the 수집 tab from either.
  it("keeps the crawler detail page under 크롤러 상태", () => {
    const matched = NAV_ITEMS.filter((item) => isActiveNav("/crawlers/bfr-bund-de-en", item.match));
    expect(matched.map((item) => item.label)).toEqual(["크롤러 상태"]);
  });
});

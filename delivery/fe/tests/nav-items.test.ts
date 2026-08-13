import { describe, expect, it } from "vitest";
import { isActiveNav, NAV_ITEMS } from "../src/lib/nav-items";

describe("isActiveNav", () => {
  it("matches the root route only exactly", () => {
    expect(isActiveNav("/", ["/", "/crawlers"])).toBe(true);
    expect(isActiveNav("/documents", ["/", "/crawlers"])).toBe(false);
  });

  it("matches a section and its nested routes", () => {
    expect(isActiveNav("/crawlers/abc", ["/", "/crawlers"])).toBe(true);
    expect(isActiveNav("/documents", ["/documents"])).toBe(true);
    expect(isActiveNav("/documents/12", ["/documents"])).toBe(true);
  });

  it("does not match a route that merely starts with the same characters", () => {
    expect(isActiveNav("/documentsxyz", ["/documents"])).toBe(false);
    expect(isActiveNav("/schedules-archive", ["/schedules"])).toBe(false);
  });
});

describe("NAV_ITEMS", () => {
  it("keeps the four console sections in order", () => {
    expect(NAV_ITEMS.map((item) => item.href)).toEqual(["/", "/schedules", "/documents", "/translations"]);
    expect(NAV_ITEMS.map((item) => item.label)).toEqual(["크롤러 상태", "수집 예약", "문서 탐색", "번역 작업"]);
  });

  it("activates exactly one section for every console route", () => {
    const routes = ["/", "/crawlers/site-1", "/schedules", "/documents", "/documents/9", "/translations"];
    for (const route of routes) {
      const matched = NAV_ITEMS.filter((item) => isActiveNav(route, item.match));
      expect(matched, route).toHaveLength(1);
    }
  });
});

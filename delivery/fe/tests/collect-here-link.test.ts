import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { siteCollectHref } from "../src/lib/nav-items";

const read = (path: string): string => readFileSync(join(__dirname, "..", path), "utf8");

describe("collection starts only on 사이트 수집", () => {
  it("links a crawler to its 사이트 수집 detail", () => {
    expect(siteCollectHref("canada-ca-en")).toBe("/site-collect?site=canada-ca-en");
    expect(siteCollectHref("a b/c")).toBe("/site-collect?site=a%20b%2Fc");
    expect(siteCollectHref()).toBe("/site-collect");
  });

  // The crawler screens show the link instead of their run buttons; the
  // panels themselves remain for the password-protected job dashboard.
  it("keeps run panels off the crawler list and crawler detail", () => {
    for (const page of ["src/app/(shell)/crawlers/page.tsx", "src/app/crawlers/[siteId]/page.tsx"]) {
      const source = read(page);
      expect(source, page).not.toMatch(/<(Bulk)?RunPanel\b/);
      expect(source, page).toContain("<CollectHereLink");
    }
    expect(read("src/app/(shell)/page.tsx")).toMatch(/<RunPanel\b/);
  });
});

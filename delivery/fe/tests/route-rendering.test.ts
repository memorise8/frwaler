import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// A server page that reads live data must opt out of static generation, and
// `next build` is the only place the omission shows up -- the dev server
// renders every request dynamically, so the console looks perfect right up
// until the delivery image is built.
//
// This was not hypothetical. `/schedules` shipped without the flag and the
// production build failed on it: prerendering ran getCrawlerHealth(), whose
// audited CSV is copied into the runtime image but not the build stage. The
// quieter half is worse -- had the CSV been present, the build would have
// succeeded and baked the page's build-time backend fetch (which cannot
// succeed, no backend exists during a build) into a permanent "연결 안 됨"
// screen that no restart or redeploy would clear.

const APP_DIR = fileURLToPath(new URL("../src/app", import.meta.url));

const pageFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return pageFiles(path);
    return entry.name === "page.tsx" ? [path] : [];
  });

const pages = pageFiles(APP_DIR).map((path) => ({
  route: path.slice(APP_DIR.length).replace(/\\/g, "/"),
  source: readFileSync(path, "utf8"),
}));

// Anything that reaches outside the render for its content: awaiting a
// backend, reading the catalogue off disk, or asking for the request URL.
const READS_SERVER_DATA = /export default async function|process\.env\.BE_URL|getCrawlerHealth|getDatabaseStats|searchParams/;

describe("app router pages", () => {
  it("finds the routes it is meant to be guarding", () => {
    expect(pages.length).toBeGreaterThanOrEqual(8);
    expect(pages.map((page) => page.route)).toContain("/(shell)/schedules/page.tsx");
  });

  it.each(pages.filter((page) => READS_SERVER_DATA.test(page.source)))(
    "$route declares force-dynamic",
    ({ source }) => {
      expect(source).toMatch(/export const dynamic\s*=\s*"force-dynamic"/);
    },
  );

  // The counterpart: a page with no server data is legitimately static, and
  // demanding the flag everywhere would make the rule meaningless.
  it("leaves a page that renders only client components alone", () => {
    const translations = pages.find((page) => page.route === "/(shell)/translations/page.tsx");
    expect(translations).toBeDefined();
    expect(READS_SERVER_DATA.test(translations!.source)).toBe(false);
  });
});

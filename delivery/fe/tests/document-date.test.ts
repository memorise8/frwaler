import { describe, expect, it } from "vitest";
import { documentDate } from "../src/lib/document-date";

describe("source date display", () => {
  it("preserves the calendar day and keeps unresolved source text visible", () => {
    expect(documentDate("2026-08-07", "07.08.2026", "normalized")).toBe("2026-08-07");
    expect(documentDate(null, "07.08.2026", "ambiguous")).toBe("07.08.2026 (날짜 확인 필요)");
    expect(documentDate(null, "2026-08", "partial")).toBe("2026-08 (부분 날짜)");
    expect(documentDate("2026-02-29", null, "legacy_unchecked")).toContain("날짜 확인 필요");
    expect(documentDate("07.08.2026")).toBe("07.08.2026 (날짜 확인 필요)");
    expect(documentDate(null)).toBe("날짜 미상");
  });
});

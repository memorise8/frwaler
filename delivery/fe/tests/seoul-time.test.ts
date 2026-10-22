import { describe, expect, it } from "vitest";
import { formatSeoul, formatSeoulDate } from "../src/lib/seoul-time";

describe("seoul-time", () => {
  it("formats UTC instants in Asia/Seoul whatever the host zone", () => {
    expect(formatSeoul("2026-10-21T15:05:00Z")).toBe("2026-10-22 00:05");
    expect(formatSeoul("2026-10-21 15:05:09+00", { seconds: true })).toBe("2026-10-22 00:05:09");
    expect(formatSeoulDate("2026-10-21T15:05:00Z")).toBe("2026-10-22");
  });
  it("falls back on missing or bad values", () => {
    expect(formatSeoul(null)).toBe("-");
    expect(formatSeoul("nope", { fallback: "알 수 없음" })).toBe("알 수 없음");
    expect(formatSeoulDate(undefined)).toBe("-");
  });
});

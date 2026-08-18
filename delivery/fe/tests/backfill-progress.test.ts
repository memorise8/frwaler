import { describe, expect, it } from "vitest";
import { backfillStateLabel, formatBackfillPercent } from "@/lib/backfill-progress";

describe("formatBackfillPercent", () => {
  it("formats a sane ratio with one decimal", () => {
    expect(formatBackfillPercent(1_337_305, 13_373_055)).toBe("10.0%");
  });
  it("caps display at 100% even when estimate is stale", () => {
    // total_estimate 는 추정치다 -- 초과 수집이 120% 로 보이면 신뢰를 잃는다.
    expect(formatBackfillPercent(150, 100)).toBe("100%");
  });
  it("returns null without a usable estimate", () => {
    expect(formatBackfillPercent(10, null)).toBeNull();
    expect(formatBackfillPercent(10, 0)).toBeNull();
    expect(formatBackfillPercent(-1, 100)).toBeNull();
  });
});

describe("backfillStateLabel", () => {
  it("distinguishes the three states", () => {
    expect(backfillStateLabel({ cursor: null, completed_at: null })).toBe("시작 전");
    expect(backfillStateLabel({ cursor: { page: 4 }, completed_at: null })).toBe("진행 중");
    expect(backfillStateLabel({ cursor: { page: 4 }, completed_at: "2026-08-18T00:00:00Z" })).toBe("완주");
  });
});

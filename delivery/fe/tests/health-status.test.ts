import { describe, expect, it } from "vitest";
import { formatHealthDate, healthLabel, healthTone } from "../src/lib/health-status";

describe("health evidence presentation", () => {
  it("uses deterministic Korean time across server and browser locales", () => {
    expect(formatHealthDate("2026-10-07T12:00:00Z")).toBe("2026-10-07 21:00:00");
    expect(formatHealthDate(null)).toBe("검사 기록 없음");
    expect(formatHealthDate("invalid")).toBe("시각 확인 불가");
  });
  it("does not display missing or expired evidence as a healthy or failed crawl", () => {
    for (const state of ["unchecked", "stale", "code_changed", "empty", "configuration", "unsupported"]) {
      expect(healthTone(state)).toBe("unknown");
    }
    expect(healthTone("healthy")).toBe("healthy");
    expect(healthTone("blocked")).toBe("unhealthy");
  });

  it("labels partial samples and unknown future states without claiming success", () => {
    expect(healthLabel("partial")).toBe("일부 성공·확인 필요");
    expect(healthLabel("future_state")).toBe("알 수 없음");
    expect(healthTone("future_state")).toBe("unknown");
  });
});

import { describe, expect, it } from "vitest";
import { metricLabel, metricValue, workerStatusLabel } from "../src/lib/translation-labels";

describe("translation operation labels", () => {
  it("names metrics in Korean and keeps unknown codes", () => {
    expect(metricLabel("disk_free_bytes")).toBe("디스크 남은 공간");
    expect(metricLabel("endpoint_healthy")).toBe("번역 서버 응답");
    expect(metricLabel("new_metric")).toBe("new_metric");
  });

  it("formats values by unit", () => {
    expect(metricValue("disk_free_bytes", 5 * 1024 ** 3, null)).toBe("5.0 GiB");
    expect(metricValue("disk_free_bytes", 3 * 1024 ** 4, null)).toBe("3.0 TiB");
    expect(metricValue("gpu_utilization_percent", 42.4, null)).toBe("42%");
    expect(metricValue("endpoint_healthy", null, true)).toBe("정상");
    expect(metricValue("endpoint_healthy", null, false)).toBe("응답 없음");
    expect(metricValue("disk_free_bytes", null, null)).toBe("-");
  });

  it("translates worker states", () => {
    expect(workerStatusLabel("idle")).toBe("대기 중");
    expect(workerStatusLabel("weird")).toBe("weird");
  });
});

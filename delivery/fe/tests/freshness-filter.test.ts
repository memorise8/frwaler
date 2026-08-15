import { describe, expect, it } from "vitest";
import { FRESHNESS_FILTER_VALUES, matchesFreshnessFilter, OVER_90_OR_NEVER } from "../src/lib/freshness-filter";

describe("matchesFreshnessFilter", () => {
  it("matches a plain bucket against its own filter value", () => {
    expect(matchesFreshnessFilter("within_7_days", "within_7_days")).toBe(true);
    expect(matchesFreshnessFilter("8_to_30_days", "8_to_30_days")).toBe(true);
    expect(matchesFreshnessFilter("31_to_90_days", "31_to_90_days")).toBe(true);
  });

  it("does not match a different plain bucket", () => {
    expect(matchesFreshnessFilter("within_7_days", "8_to_30_days")).toBe(false);
    expect(matchesFreshnessFilter("31_to_90_days", "within_7_days")).toBe(false);
  });

  it("combines over_90_days and never under the over_90_or_never filter", () => {
    expect(matchesFreshnessFilter("over_90_days", OVER_90_OR_NEVER)).toBe(true);
    expect(matchesFreshnessFilter("never", OVER_90_OR_NEVER)).toBe(true);
  });

  it("does not let a fresher bucket slip into the combined filter", () => {
    expect(matchesFreshnessFilter("within_7_days", OVER_90_OR_NEVER)).toBe(false);
    expect(matchesFreshnessFilter("8_to_30_days", OVER_90_OR_NEVER)).toBe(false);
    expect(matchesFreshnessFilter("31_to_90_days", OVER_90_OR_NEVER)).toBe(false);
  });

  it("treats an unknown bucket value as never matching", () => {
    expect(matchesFreshnessFilter("unknown_bucket", "within_7_days")).toBe(false);
    expect(matchesFreshnessFilter("unknown_bucket", OVER_90_OR_NEVER)).toBe(false);
  });

  it("treats an unknown filter value as never matching a known bucket", () => {
    expect(matchesFreshnessFilter("within_7_days", "unknown_filter")).toBe(false);
    expect(matchesFreshnessFilter("over_90_days", "unknown_filter")).toBe(false);
  });
});

describe("FRESHNESS_FILTER_VALUES", () => {
  it("lists all four card buckets in card order", () => {
    expect(FRESHNESS_FILTER_VALUES).toEqual(["within_7_days", "8_to_30_days", "31_to_90_days", OVER_90_OR_NEVER]);
  });
});

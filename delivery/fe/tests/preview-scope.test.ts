import { describe, expect, it } from "vitest";
import { requestedPreviewScope } from "../src/lib/preview-scope";

describe("requestedPreviewScope", () => {
  it("is the requested limit when the population is far larger", () => {
    expect(requestedPreviewScope(536019, 20)).toBe(20);
    expect(requestedPreviewScope(1071145, 500)).toBe(500);
  });

  it("is the population when the limit exceeds it", () => {
    expect(requestedPreviewScope(12, 1000)).toBe(12);
  });

  it("is the population when the limit equals it exactly", () => {
    expect(requestedPreviewScope(20, 20)).toBe(20);
  });

  it("is zero when the population is empty", () => {
    expect(requestedPreviewScope(0, 20)).toBe(0);
  });

  it("never goes negative for a malformed limit", () => {
    expect(requestedPreviewScope(100, -5)).toBe(0);
    expect(requestedPreviewScope(100, 0)).toBe(0);
  });
});

import { describe, expect, it } from "vitest";
import { MAX_CRAWL_LIMIT, parseCrawlLimit } from "../src/lib/crawl-limit";

describe("parseCrawlLimit", () => {
  it("accepts a valid positive integer", () => {
    expect(parseCrawlLimit(3)).toEqual({ ok: true, limit: 3 });
    expect(parseCrawlLimit(100)).toEqual({ ok: true, limit: 100 });
  });

  it("accepts a valid numeric string", () => {
    expect(parseCrawlLimit("20")).toEqual({ ok: true, limit: 20 });
  });

  it("accepts the cap itself", () => {
    expect(parseCrawlLimit(MAX_CRAWL_LIMIT)).toEqual({ ok: true, limit: MAX_CRAWL_LIMIT });
  });

  it("rejects zero", () => {
    const result = parseCrawlLimit(0);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("1 이상");
  });

  it("rejects an empty string", () => {
    const result = parseCrawlLimit("");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("rejects a whitespace-only string", () => {
    const result = parseCrawlLimit("   ");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("rejects null", () => {
    const result = parseCrawlLimit(null);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("rejects undefined", () => {
    const result = parseCrawlLimit(undefined);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("rejects a negative number", () => {
    const result = parseCrawlLimit(-5);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("1 이상");
  });

  it("rejects a negative numeric string", () => {
    const result = parseCrawlLimit("-1");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("1 이상");
  });

  it("rejects a non-integer", () => {
    const result = parseCrawlLimit(2.5);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("정수");
  });

  it("rejects a value above the cap", () => {
    const result = parseCrawlLimit(MAX_CRAWL_LIMIT + 1);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain(String(MAX_CRAWL_LIMIT));
  });

  it("rejects NaN", () => {
    const result = parseCrawlLimit(NaN);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("숫자");
  });

  it("rejects a non-numeric string", () => {
    const result = parseCrawlLimit("abc");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("숫자");
  });
});

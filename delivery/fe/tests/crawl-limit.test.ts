import { describe, expect, it } from "vitest";
import { MAX_CRAWL_LIMIT, parseCrawlLimit, parseScheduleLimit, UNBOUNDED_CRAWL_LIMIT } from "../src/lib/crawl-limit";

describe("parseCrawlLimit", () => {
  it("accepts the explicit unbounded sentinel as limit: null", () => {
    expect(parseCrawlLimit(UNBOUNDED_CRAWL_LIMIT)).toEqual({ ok: true, limit: null });
  });

  it("does not treat a missing limit as unbounded", () => {
    const result = parseCrawlLimit(undefined);
    expect(result).not.toEqual({ ok: true, limit: null });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("does not treat a null limit as unbounded", () => {
    const result = parseCrawlLimit(null);
    expect(result).not.toEqual({ ok: true, limit: null });
    expect(result.ok).toBe(false);
  });

  it("does not treat an empty string as unbounded", () => {
    const result = parseCrawlLimit("");
    expect(result).not.toEqual({ ok: true, limit: null });
    expect(result.ok).toBe(false);
  });

  it("does not treat zero as unbounded", () => {
    const result = parseCrawlLimit(0);
    expect(result).not.toEqual({ ok: true, limit: null });
    expect(result.ok).toBe(false);
  });

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

describe("parseScheduleLimit", () => {
  // A schedule fires unattended, repeatedly, forever. Unlike the run
  // panel's one-off unbounded choice — which the operator re-confirms by
  // being present at that single execution — a schedule's "confirmation"
  // only ever happens once, at creation, and then replays automatically on
  // every future run with nobody watching. So this console does not offer
  // recurring unbounded crawls at all: the sentinel that parseCrawlLimit
  // accepts must be refused here.
  it("rejects the explicit unbounded sentinel that parseCrawlLimit accepts", () => {
    expect(parseCrawlLimit(UNBOUNDED_CRAWL_LIMIT)).toEqual({ ok: true, limit: null });
    const result = parseScheduleLimit(UNBOUNDED_CRAWL_LIMIT);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("무제한");
  });

  it("still does not treat a missing limit as unbounded", () => {
    const result = parseScheduleLimit(undefined);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("still does not treat a blank string as unbounded", () => {
    const result = parseScheduleLimit("");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("입력");
  });

  it("still rejects zero", () => {
    const result = parseScheduleLimit(0);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("1 이상");
  });

  it("still rejects a negative number", () => {
    const result = parseScheduleLimit(-1);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("1 이상");
  });

  it("still rejects a value above the shared cap", () => {
    const result = parseScheduleLimit(1500);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain(String(MAX_CRAWL_LIMIT));
  });

  it("accepts a valid bounded integer, matching parseCrawlLimit", () => {
    expect(parseScheduleLimit(100)).toEqual({ ok: true, limit: 100 });
    expect(parseScheduleLimit("250")).toEqual({ ok: true, limit: 250 });
  });

  it("accepts the cap itself", () => {
    expect(parseScheduleLimit(MAX_CRAWL_LIMIT)).toEqual({ ok: true, limit: MAX_CRAWL_LIMIT });
  });
});

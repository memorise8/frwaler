import { describe, expect, it } from "vitest";
import {
  BULK_LIMIT_MAX,
  classifyBulkOutcome,
  describeBulkCancel,
  describeBulkRun,
  MAX_BULK_SITES,
  parseBulkRunRequest,
  summarizeBulkRun,
  type BulkOutcome,
} from "../src/lib/bulk-run";
import { UNBOUNDED_CRAWL_LIMIT } from "../src/lib/crawl-limit";

const ok = (body: unknown) => {
  const result = parseBulkRunRequest(body);
  if (!result.ok) throw new Error(`expected ok, got: ${result.error}`);
  return result.value;
};
const err = (body: unknown) => {
  const result = parseBulkRunRequest(body);
  if (result.ok) throw new Error("expected a rejection");
  return result.error;
};

describe("parseBulkRunRequest", () => {
  it("accepts a well-formed request", () => {
    expect(ok({ siteIds: ["a-site", "b.site"], mode: "full", limit: 50 }))
      .toEqual({ siteIds: ["a-site", "b.site"], mode: "full", limit: 50 });
  });

  it("defaults the mode to incremental", () => {
    expect(ok({ siteIds: ["a"], limit: 1 }).mode).toBe("incremental");
  });

  it("deduplicates and trims the site list", () => {
    expect(ok({ siteIds: [" a ", "a", "b", ""], limit: 5 }).siteIds).toEqual(["a", "b"]);
  });

  // A bulk sweep is one confirmation covering hundreds of third-party sites.
  // Unbounded collection stays a single-site decision, so the sentinel that
  // expresses it must be refused here by name rather than coerced to NaN.
  it("refuses the unbounded sentinel with a reason that names it", () => {
    expect(err({ siteIds: ["a"], limit: UNBOUNDED_CRAWL_LIMIT })).toContain("무제한");
  });

  it("requires an integer limit inside the backend's accepted range", () => {
    expect(err({ siteIds: ["a"], limit: 0 })).toContain("사이트당");
    expect(err({ siteIds: ["a"], limit: BULK_LIMIT_MAX + 1 })).toContain("사이트당");
    expect(err({ siteIds: ["a"], limit: 2.5 })).toContain("사이트당");
    expect(err({ siteIds: ["a"], limit: "" })).toContain("사이트당");
    expect(err({ siteIds: ["a"] })).toContain("사이트당");
  });

  it("rejects an empty or oversized target list", () => {
    expect(err({ siteIds: [], limit: 5 })).toContain("한 개도 없습니다");
    const many = Array.from({ length: MAX_BULK_SITES + 1 }, (_, index) => `s${index}`);
    expect(err({ siteIds: many, limit: 5 })).toContain("최대");
  });

  it("accepts exactly the cap", () => {
    const many = Array.from({ length: MAX_BULK_SITES }, (_, index) => `s${index}`);
    expect(ok({ siteIds: many, limit: 5 }).siteIds).toHaveLength(MAX_BULK_SITES);
  });

  // Matches the backend's site_id pattern so a bad id costs no round trip.
  it("rejects a site id the backend would reject", () => {
    expect(err({ siteIds: ["ok", "bad id"], limit: 5 })).toContain("bad id");
    expect(err({ siteIds: ["a/b"], limit: 5 })).toContain("a/b");
  });

  it("rejects a malformed body and a non-string entry", () => {
    expect(err(null)).toContain("요청 형식");
    expect(err({ limit: 5 })).toContain("목록이 없습니다");
    expect(err({ siteIds: ["a", 7], limit: 5 })).toContain("문자열");
  });

  it("rejects an unknown mode instead of silently downgrading it", () => {
    expect(err({ siteIds: ["a"], mode: "everything", limit: 5 })).toContain("수집 모드");
  });
});

describe("classifyBulkOutcome", () => {
  // One active job per site is a backend invariant (ActiveJobError), so 409
  // means "already working on it" -- skipped, not failed. This is what makes
  // re-running a sweep safe.
  it("treats 409 as skipped rather than failed", () => {
    expect(classifyBulkOutcome("a", 409, { detail: "site already has an active job" }))
      .toEqual({ siteId: "a", kind: "skipped", reason: "이미 실행 중인 작업이 있습니다." });
  });

  it("records a queued job with its id", () => {
    expect(classifyBulkOutcome("a", 200, { id: 12 })).toEqual({ siteId: "a", kind: "queued", jobId: 12 });
  });

  it("fails a success response that carries no job id", () => {
    expect(classifyBulkOutcome("a", 200, {})).toMatchObject({ kind: "failed" });
  });

  it("explains a missing site and passes through other backend details", () => {
    expect(classifyBulkOutcome("a", 404, {})).toMatchObject({ kind: "failed", reason: "백엔드에 등록되지 않은 사이트입니다." });
    expect(classifyBulkOutcome("a", 422, { detail: "bad input" })).toMatchObject({ kind: "failed", reason: "bad input" });
    expect(classifyBulkOutcome("a", 500, null)).toMatchObject({ kind: "failed", reason: "HTTP 500" });
  });
});

describe("summarizeBulkRun / describeBulkRun", () => {
  const outcomes: BulkOutcome[] = [
    { siteId: "a", kind: "queued", jobId: 1 },
    { siteId: "b", kind: "queued", jobId: 2 },
    { siteId: "c", kind: "skipped", reason: "이미 실행 중" },
    { siteId: "d", kind: "failed", reason: "boom" },
  ];

  it("counts each outcome kind", () => {
    expect(summarizeBulkRun(outcomes)).toEqual({ queued: 2, skipped: 1, failed: 1 });
  });

  it("mentions only the categories that occurred", () => {
    expect(describeBulkRun({ queued: 5, skipped: 0, failed: 0 })).toBe("등록 5건");
    expect(describeBulkRun({ queued: 5, skipped: 2, failed: 1 })).toContain("건너뜀 2건(이미 실행 중)");
  });
});

describe("describeBulkCancel", () => {
  // A queued job is cancelled outright; a running one only receives the
  // request and keeps collecting until its next checkpoint. Reporting both as
  // "cancelled" would tell an operator the crawl stopped when it has not.
  it("separates a completed cancellation from a requested one", () => {
    const text = describeBulkCancel({ cancelled: 40, requested: 1, failed: 0 });
    expect(text).toContain("대기 작업 40건 취소");
    expect(text).toContain("실행 중 1건은 현재 수집 단위가 끝나면 중단됩니다");
  });

  it("says so plainly when there was nothing to cancel", () => {
    expect(describeBulkCancel({ cancelled: 0, requested: 0, failed: 0 })).toBe("취소할 작업이 없습니다.");
  });
});

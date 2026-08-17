import { describe, expect, it } from "vitest";
import { formatJobDuration } from "../src/lib/job-duration";

const at = (seconds: number): string => new Date(Date.UTC(2026, 7, 17, 12, 0, 0) + seconds * 1000).toISOString();

describe("formatJobDuration", () => {
  // The real case that motivated this: justice-govt-nz ran 3m54s against an
  // AWS WAF block and the console showed 0초, because both timestamps came
  // from the same transaction.
  it("formats a minutes-long run", () => {
    expect(formatJobDuration(at(0), at(234))).toBe("3분 54초");
  });

  it("drops the seconds part on a whole minute", () => {
    expect(formatJobDuration(at(0), at(120))).toBe("2분");
  });

  it("formats seconds and hours", () => {
    expect(formatJobDuration(at(0), at(62))).toBe("1분 2초");
    expect(formatJobDuration(at(0), at(12))).toBe("12초");
    expect(formatJobDuration(at(0), at(3600))).toBe("1시간");
    expect(formatJobDuration(at(0), at(4500))).toBe("1시간 15분");
  });

  // "0초" is precisely the reading the old bug produced, so a genuinely
  // instant job must not be reported the same way a broken measurement was.
  it("never prints 0초 for a sub-second run", () => {
    expect(formatJobDuration("2026-08-17T12:00:00.100Z", "2026-08-17T12:00:00.900Z")).toBe("1초 미만");
  });

  // Identical stamps are the signature of the transaction-timestamp bug, which
  // every job recorded before the worker fix carries -- job #8 ran 3m54s and
  // stored 12:01:57.448963 for both. Printing "1초 미만" would repeat the
  // original lie, so an unmoved clock reads as unmeasured.
  it("treats byte-identical stamps as unmeasured, not instant", () => {
    expect(formatJobDuration(at(0), at(0))).toBeNull();
    expect(formatJobDuration("2026-08-17T12:01:57.448963Z", "2026-08-17T12:01:57.448963Z")).toBeNull();
  });

  it("returns null when the job has not finished or a stamp is missing", () => {
    expect(formatJobDuration(at(0), null)).toBeNull();
    expect(formatJobDuration(null, at(10))).toBeNull();
    expect(formatJobDuration(undefined, undefined)).toBeNull();
    expect(formatJobDuration("", "")).toBeNull();
  });

  it("returns null rather than a number it cannot trust", () => {
    expect(formatJobDuration("not a date", at(10))).toBeNull();
    expect(formatJobDuration(at(10), "not a date")).toBeNull();
    expect(formatJobDuration(at(10), at(0))).toBeNull();
  });

  it("rounds to the nearest second", () => {
    expect(formatJobDuration("2026-08-17T12:00:00.000Z", "2026-08-17T12:00:11.600Z")).toBe("12초");
  });
});

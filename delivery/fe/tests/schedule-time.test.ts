import { afterEach, describe, expect, it } from "vitest";
import { formatScheduleTimestamp } from "../src/lib/schedule-time";

// There are currently 0 rows in crawl_schedules, so the hydration mismatch
// this module fixes has never actually been rendered live -- it can only
// be exercised here, against representative row payloads, by simulating
// the one condition that causes it: the server process and the browser
// process resolving Date#toLocaleString against different local
// timezones. process.env.TZ is the standard way to move Node's own
// notion of "local timezone" for exactly this kind of test.
describe("formatScheduleTimestamp", () => {
  const originalTz = process.env.TZ;

  afterEach(() => {
    process.env.TZ = originalTz;
  });

  it("renders a readable Korean-locale local time, not a raw ISO string", () => {
    const formatted = formatScheduleTimestamp("2026-01-01T16:30:00.000Z");
    expect(formatted).not.toBe("2026-01-01T16:30:00.000Z");
    expect(formatted).toContain("2026");
  });

  it("demonstrates the bug: an unpinned toLocaleString diverges across host timezones", () => {
    // This is the exact call the component used to make. It is asserted
    // here, not as a claim about this module, but as proof the underlying
    // defect is real and reproducible in this environment -- process.env.TZ
    // measurably changes what an unpinned call produces for the same
    // instant, which is precisely why SSR (container, UTC) and hydration
    // (operator's machine, KST) rendered different markup.
    const iso = "2026-01-01T16:30:00.000Z";
    process.env.TZ = "UTC";
    const asUtc = new Date(iso).toLocaleString("ko-KR");
    process.env.TZ = "Asia/Seoul";
    const asSeoul = new Date(iso).toLocaleString("ko-KR");
    expect(asUtc).not.toBe(asSeoul);
  });

  it("stays identical across differing host timezones, unlike the call it replaces", () => {
    const iso = "2026-01-01T16:30:00.000Z";
    process.env.TZ = "UTC";
    const asUtc = formatScheduleTimestamp(iso);
    process.env.TZ = "America/Los_Angeles";
    const asLosAngeles = formatScheduleTimestamp(iso);
    process.env.TZ = "Asia/Seoul";
    const asSeoul = formatScheduleTimestamp(iso);
    expect(asUtc).toBe(asLosAngeles);
    expect(asLosAngeles).toBe(asSeoul);
  });

  it("stays identical across timezones even for a payload that crosses a calendar day in KST", () => {
    // 2026-01-01T16:30:00Z is still 2026-01-01 under UTC but already
    // 2026-01-02 under Asia/Seoul (UTC+9) -- the sharpest form of the
    // divergence, since the date portion itself would differ, not just
    // the hour.
    const iso = "2026-01-01T16:30:00.000Z";
    process.env.TZ = "UTC";
    const asUtc = formatScheduleTimestamp(iso);
    process.env.TZ = "Pacific/Kiritimati"; // UTC+14, as far from KST as timezones get
    const asKiritimati = formatScheduleTimestamp(iso);
    expect(asUtc).toBe(asKiritimati);
    expect(asUtc).toContain("2026. 1. 2.");
  });

  it("formats a null-less next_run_at (always present on a real row) without throwing", () => {
    expect(() => formatScheduleTimestamp("2026-08-20T09:00:00.000Z")).not.toThrow();
  });

  it("falls back to a plain Korean label for an unparsable timestamp instead of throwing or printing 'Invalid Date'", () => {
    const formatted = formatScheduleTimestamp("not-a-date");
    expect(formatted).toBe("알 수 없음");
  });
});

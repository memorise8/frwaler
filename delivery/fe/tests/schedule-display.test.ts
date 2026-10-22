import { describe, expect, it } from "vitest";
import {
  formatScheduleInterval,
  formatScheduleMode,
  isRunnerScheduleMode,
  isScheduleDirty,
  needsTrialConfirm,
  pendingScheduleSaves,
  scheduleCreateBody,
  setScheduleDraft,
  shownEnabled,
} from "../src/lib/schedule-display";

const row = (site_id: string, enabled: boolean) => ({ site_id, interval_hours: 168, mode: "incremental", limit_n: 100, enabled });

describe("formatScheduleInterval", () => {
  it("names the usual periods in plain Korean", () => {
    expect(formatScheduleInterval(24)).toBe("매일");
    expect(formatScheduleInterval(168)).toBe("매주(7일)");
    expect(formatScheduleInterval(720)).toBe("매월(30일)");
  });

  it("falls back to days, then hours", () => {
    expect(formatScheduleInterval(48)).toBe("2일마다");
    expect(formatScheduleInterval(6)).toBe("6시간마다");
    expect(formatScheduleInterval(0)).toBe("알 수 없음");
  });
});

describe("formatScheduleMode", () => {
  it("explains incremental mode and passes unknown modes through", () => {
    expect(formatScheduleMode("incremental")).toBe("증분(새 글만)");
    expect(formatScheduleMode("something")).toBe("something");
  });
});

// The 사용 checkbox must not be decorative: changing it marks the row dirty
// and the save button sends exactly that change.
describe("schedule checkbox drafts", () => {
  const rows = [row("a-site", true), row("b-site", false), row("c-site", true)];

  it("marks a row dirty when its checkbox changes and shows the unsaved value", () => {
    const drafts = setScheduleDraft({}, rows[0]!, false);
    expect(isScheduleDirty(drafts, rows[0]!)).toBe(true);
    expect(shownEnabled(drafts, rows[0]!)).toBe(false);
    expect(isScheduleDirty(drafts, rows[1]!)).toBe(false);
    expect(shownEnabled(drafts, rows[1]!)).toBe(false);
  });

  it("clears the dirty mark when the checkbox goes back to the saved value", () => {
    const drafts = setScheduleDraft(setScheduleDraft({}, rows[0]!, false), rows[0]!, true);
    expect(drafts).toEqual({});
    expect(pendingScheduleSaves(rows, drafts)).toEqual([]);
  });

  it("turns every dirty row into one save carrying the new enabled value and the saved settings", () => {
    let drafts = setScheduleDraft({}, rows[0]!, false);
    drafts = setScheduleDraft(drafts, rows[1]!, true);
    expect(pendingScheduleSaves(rows, drafts)).toEqual([
      { siteId: "a-site", body: { interval_hours: 168, mode: "incremental", limit_n: 100, enabled: false } },
      { siteId: "b-site", body: { interval_hours: 168, mode: "incremental", limit_n: 100, enabled: true } },
    ]);
  });

  it("is no longer dirty once the saved row comes back with the drafted value", () => {
    const drafts = setScheduleDraft({}, rows[0]!, false);
    const savedRow = row("a-site", false);
    expect(setScheduleDraft(drafts, savedRow, savedRow.enabled)).toEqual({});
  });
});

describe("enum_incremental schedules (new plan-driven collector)", () => {
  it("is labelled and routed to the collect runner", () => {
    expect(formatScheduleMode("enum_incremental")).toBe("새 수집기 증분(새 글만)");
    expect(isRunnerScheduleMode("enum_incremental")).toBe(true);
    expect(isRunnerScheduleMode("incremental")).toBe(false);
  });

  it("never carries a 1회 제한 (the plan bounds it) and keeps the registered-crawler body unchanged", () => {
    expect(scheduleCreateBody("enum_incremental", 24, 100)).toEqual({ interval_hours: 24, mode: "enum_incremental", limit_n: null, enabled: true });
    expect(scheduleCreateBody("incremental", 168, 100)).toEqual({ interval_hours: 168, mode: "incremental", limit_n: 100, enabled: true });
  });

  it("needs the unverified-crawler trial confirmation only for registered-crawler schedules", () => {
    expect(needsTrialConfirm({ enabled: true, mode: "incremental" })).toBe(true);
    expect(needsTrialConfirm({ enabled: true })).toBe(true);
    expect(needsTrialConfirm({ enabled: true, mode: "enum_incremental" })).toBe(false);
    expect(needsTrialConfirm({ enabled: false, mode: "incremental" })).toBe(false);
  });
});

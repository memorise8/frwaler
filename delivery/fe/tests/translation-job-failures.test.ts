import { describe, expect, it } from "vitest";
import { detectConfigurationFailures } from "../src/lib/translation-job-failures";

describe("detectConfigurationFailures", () => {
  it("is false for an empty job list", () => {
    expect(detectConfigurationFailures([])).toEqual({ hasConfigurationFailures: false, failingCount: 0 });
  });

  it("is false when no job has an error_code", () => {
    const jobs = [{ status: "pending", error_code: null }, { status: "completed", error_code: null }];
    expect(detectConfigurationFailures(jobs)).toEqual({ hasConfigurationFailures: false, failingCount: 0 });
  });

  it("is false for failures with a different error_code", () => {
    const jobs = [{ status: "failed", error_code: "timeout" }, { status: "failed", error_code: "auth" }];
    expect(detectConfigurationFailures(jobs)).toEqual({ hasConfigurationFailures: false, failingCount: 0 });
  });

  it("ignores a configuration error_code on a non-failed job", () => {
    const jobs = [{ status: "pending", error_code: "configuration" }];
    expect(detectConfigurationFailures(jobs)).toEqual({ hasConfigurationFailures: false, failingCount: 0 });
  });

  it("is true and counts jobs failed for a configuration reason", () => {
    const jobs = [
      { status: "failed", error_code: "configuration" },
      { status: "failed", error_code: "timeout" },
      { status: "completed", error_code: null },
      { status: "failed", error_code: "configuration" },
    ];
    expect(detectConfigurationFailures(jobs)).toEqual({ hasConfigurationFailures: true, failingCount: 2 });
  });

  it("treats an undefined error_code the same as null", () => {
    const jobs = [{ status: "failed" }];
    expect(detectConfigurationFailures(jobs)).toEqual({ hasConfigurationFailures: false, failingCount: 0 });
  });
});

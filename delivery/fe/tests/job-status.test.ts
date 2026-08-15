import { describe, expect, it } from "vitest";
import {
  CRAWL_JOB_STATUS_LABELS,
  describeJobOutcome,
  describeTrackingFailure,
  isTerminalJobStatus,
  jobStatusLabel,
} from "../src/lib/job-status";

describe("isTerminalJobStatus", () => {
  it("treats done, failed, cancelled as terminal", () => {
    expect(isTerminalJobStatus("done")).toBe(true);
    expect(isTerminalJobStatus("failed")).toBe(true);
    expect(isTerminalJobStatus("cancelled")).toBe(true);
  });

  it("treats queued, running, cancelling as non-terminal", () => {
    expect(isTerminalJobStatus("queued")).toBe(false);
    expect(isTerminalJobStatus("running")).toBe(false);
    expect(isTerminalJobStatus("cancelling")).toBe(false);
  });

  it("treats an unknown status as non-terminal rather than assuming completion", () => {
    expect(isTerminalJobStatus("something-new")).toBe(false);
  });
});

describe("jobStatusLabel", () => {
  it("labels every known status in Korean", () => {
    for (const status of Object.keys(CRAWL_JOB_STATUS_LABELS)) {
      expect(jobStatusLabel(status)).not.toBe(status);
    }
  });

  it("falls back to the raw status for anything unmapped", () => {
    expect(jobStatusLabel("mystery")).toBe("mystery");
  });
});

describe("describeJobOutcome", () => {
  it("explains a zero saved_count as no-new-documents, not failure", () => {
    const message = describeJobOutcome({ status: "done", saved_count: 0 });
    expect(message).toContain("0건");
    expect(message).toContain("실패가 아닙니다");
    expect(message).toContain("이 화면에서 확인할 수 없습니다");
  });

  it("treats a missing saved_count the same as zero", () => {
    const message = describeJobOutcome({ status: "done", saved_count: null });
    expect(message).toContain("0건");
  });

  it("reports a positive saved_count plainly", () => {
    expect(describeJobOutcome({ status: "done", saved_count: 3 })).toBe("신규 저장 3건입니다.");
  });

  it("surfaces the backend error text for a failed job", () => {
    const message = describeJobOutcome({ status: "failed", saved_count: 0, error: "connection refused" });
    expect(message).toContain("connection refused");
  });

  it("still says something for a failed job with no recorded error", () => {
    const message = describeJobOutcome({ status: "failed", saved_count: 0, error: null });
    expect(message).toContain("실패");
  });

  it("reports a cancelled job as cancelled", () => {
    expect(describeJobOutcome({ status: "cancelled", saved_count: 0 })).toContain("취소");
  });

  it("has nothing to say about a job that is still in flight", () => {
    expect(describeJobOutcome({ status: "running", saved_count: 0 })).toBe("");
  });
});

describe("describeTrackingFailure", () => {
  it("names a disappeared job distinctly from a generic failure", () => {
    const notFound = describeTrackingFailure(404);
    const generic = describeTrackingFailure(503);
    expect(notFound).not.toBe(generic);
    expect(notFound).toContain("찾을 수 없");
  });

  it("gives a usable message when there is no status at all (network failure)", () => {
    expect(describeTrackingFailure(null)).toContain("확인할 수 없");
  });
});

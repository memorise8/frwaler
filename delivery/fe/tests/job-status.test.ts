import { describe, expect, it } from "vitest";
import {
  CRAWL_JOB_STATUS_LABELS,
  TRUNCATED_BADGE,
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

describe("truncated runs", () => {
  // 잘린 수집도 status 는 done 이다. 배지는 "완료" 를 대체하지 않고 옆에 붙는다.
  it("keeps the done label and adds a separate badge", () => {
    expect(jobStatusLabel("done")).toBe("완료");
    expect(TRUNCATED_BADGE).toBe("부분 수집");
  });

  it("tells the operator the run was cut short and what to do", () => {
    const text = describeJobOutcome({ status: "done", saved_count: 40, truncated: true });
    expect(text).toContain("시간 제한");
    expect(text).toContain("40");
    // 재실행은 해결책이 아니다 — 크롤러는 1페이지부터 다시 걷는다.
    expect(text).toContain("시간 제한을 늘려");
    expect(text).not.toContain("이어서");
  });

  it("says so even when a truncated run saved nothing", () => {
    expect(describeJobOutcome({ status: "done", saved_count: 0, truncated: true }))
      .toContain("시간 제한");
  });

  it("leaves an ordinary completed run's text untouched", () => {
    const plain = describeJobOutcome({ status: "done", saved_count: 40 });
    expect(plain).not.toContain("시간 제한");
    expect(describeJobOutcome({ status: "done", saved_count: 40, truncated: false })).toBe(plain);
  });

  // 실패·취소는 잘림과 무관하다.
  it("does not mention truncation for failed or cancelled jobs", () => {
    expect(describeJobOutcome({ status: "failed", saved_count: 0, error: "boom", truncated: true }))
      .not.toContain("시간 제한");
    expect(describeJobOutcome({ status: "cancelled", saved_count: 0, truncated: true }))
      .not.toContain("시간 제한");
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

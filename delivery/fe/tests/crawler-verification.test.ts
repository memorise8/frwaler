import { describe, expect, it } from "vitest";
import {
  contradictsAudit,
  describeVerification,
  indexVerification,
  resolveVerificationState,
  VERIFICATION_LABEL,
  type VerificationRow,
} from "../src/lib/crawler-verification";

const row = (over: Partial<VerificationRow> = {}): VerificationRow => ({
  site_id: "a", last_status: "done", last_saved_count: 0, last_error: null,
  last_finished_at: "2026-08-17T12:00:00Z", jobs: 1, best_saved_count: 0, ...over,
});

describe("resolveVerificationState", () => {
  it("reports a site with no local history as unrun", () => {
    expect(resolveVerificationState(undefined)).toBe("unrun");
    expect(resolveVerificationState(row({ jobs: 0 }))).toBe("unrun");
  });

  // One stored document is the strongest claim this deployment can make, and
  // it outranks a later empty run: the crawler demonstrably reaches its source
  // from here, which is exactly what an IP-block verdict needs disproving.
  it("treats any past save as proof the crawler works here", () => {
    expect(resolveVerificationState(row({ best_saved_count: 5, last_saved_count: 0 }))).toBe("collected");
    expect(resolveVerificationState(row({ best_saved_count: 5, last_status: "failed" }))).toBe("collected");
  });

  it("reports a failed latest run when nothing was ever saved", () => {
    expect(resolveVerificationState(row({ last_status: "failed" }))).toBe("failed");
  });

  // A re-crawl of already-stored documents and a silently blocked fetch both
  // finish with 0 saved, so this state deliberately claims neither.
  it("keeps a completed run that saved nothing in its own state", () => {
    expect(resolveVerificationState(row({ last_status: "done" }))).toBe("ran_empty");
  });

  // Found by a real bulk run: cancelling 15 queued jobs left every one of them
  // reading "실행됨 · 신규 0건", claiming a crawl that never started.
  it("keeps a cancelled job out of the ran-empty state", () => {
    expect(resolveVerificationState(row({ last_status: "cancelled" }))).toBe("cancelled");
    expect(resolveVerificationState(row({ last_status: "cancelling" }))).toBe("cancelled");
  });

  it("still reports a site that collected before being cancelled later", () => {
    expect(resolveVerificationState(row({ last_status: "cancelled", best_saved_count: 4 }))).toBe("collected");
  });

  it("labels every state", () => {
    for (const state of ["unrun", "collected", "ran_empty", "failed", "cancelled"] as const) {
      expect(VERIFICATION_LABEL[state]).toBeTruthy();
    }
  });
});

describe("contradictsAudit", () => {
  // The two disagreements worth an operator's attention, in both directions.
  it("flags a snapshot failure that works here", () => {
    expect(contradictsAudit("unhealthy", "collected")).toBe(true);
  });

  it("flags a snapshot pass that fails here", () => {
    expect(contradictsAudit("healthy", "failed")).toBe(true);
  });

  it("does not flag agreement or an unproven state", () => {
    expect(contradictsAudit("healthy", "collected")).toBe(false);
    expect(contradictsAudit("unhealthy", "failed")).toBe(false);
    expect(contradictsAudit("unhealthy", "unrun")).toBe(false);
    expect(contradictsAudit("unhealthy", "ran_empty")).toBe(false);
    expect(contradictsAudit("healthy", "ran_empty")).toBe(false);
  });
});

describe("describeVerification", () => {
  it("says plainly that a snapshot failure works on this network", () => {
    const text = describeVerification("unhealthy", "collected", row({ jobs: 3, best_saved_count: 20 }));
    expect(text).toContain("3회 실행");
    expect(text).toContain("20건");
    expect(text).toContain("이 네트워크에서는 동작합니다");
  });

  it("does not claim a contradiction when the audit already agreed", () => {
    expect(describeVerification("healthy", "collected", row({ best_saved_count: 4 })))
      .not.toContain("납품 검증");
  });

  it("surfaces the recorded error and notes a snapshot that disagreed", () => {
    const text = describeVerification("healthy", "failed", row({ last_status: "failed", last_error: "HTTPError: 403" }));
    expect(text).toContain("HTTPError: 403");
    expect(text).toContain("납품 검증은 정상이었습니다");
  });

  // The console must not turn 0 saved into a verdict; it points at the evidence.
  it("sends an empty run to the crawler log rather than judging it", () => {
    expect(describeVerification("healthy", "ran_empty", row())).toContain("크롤러 로그");
  });

  it("says a cancelled job may never have started", () => {
    expect(describeVerification("healthy", "cancelled", row({ last_status: "cancelled" })))
      .toContain("크롤러는 시작되지 않았습니다");
  });

  it("says the snapshot is all there is when nothing ran here", () => {
    expect(describeVerification("unhealthy", "unrun", undefined)).toContain("실행한 적이 없습니다");
  });
});

describe("indexVerification", () => {
  it("keys rows by site and tolerates a missing payload", () => {
    expect(indexVerification([row({ site_id: "x" })]).get("x")?.site_id).toBe("x");
    expect(indexVerification(undefined).size).toBe(0);
  });
});

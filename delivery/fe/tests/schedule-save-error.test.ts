import { describe, expect, it } from "vitest";
import {
  describeScheduleSaveFailure,
  SCHEDULE_BACKEND_UNREACHABLE_MESSAGE,
  SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE,
} from "../src/lib/schedule-save-error";

describe("describeScheduleSaveFailure", () => {
  it("passes an already-Korean detail straight through unchanged", () => {
    // This is exactly what the route's own validation produces --
    // verified live: PUT with limit_n omitted -> 400
    // {"detail":"최대 저장 건수를 입력해야 합니다."}.
    const message = describeScheduleSaveFailure(400, { detail: "최대 저장 건수를 입력해야 합니다." });
    expect(message).toBe("최대 저장 건수를 입력해야 합니다.");
  });

  it("passes through the over-cap message verified live for limit_n: 1500", () => {
    const message = describeScheduleSaveFailure(400, { detail: "최대 저장 건수는 1000건을 넘을 수 없습니다." });
    expect(message).toBe("최대 저장 건수는 1000건을 넘을 수 없습니다.");
  });

  it("passes through the unbounded-rejection message verified live for limit_n: 'unbounded'", () => {
    const message = describeScheduleSaveFailure(400, {
      detail: "예약 수집은 무제한을 허용하지 않습니다. 최대 저장 건수를 1~1000 사이로 입력하세요.",
    });
    expect(message).toContain("무제한");
  });

  it("translates the backend's 404 site-not-found detail into actionable Korean", () => {
    const message = describeScheduleSaveFailure(404, { detail: "site not found" });
    expect(message).not.toBe("site not found");
    expect(message).toContain("사이트");
  });

  it("translates the backend's 401 auth-required detail distinctly from site-not-found", () => {
    const message = describeScheduleSaveFailure(401, { detail: "operator authentication required" });
    expect(message).not.toBe("operator authentication required");
    expect(message).toContain("인증");
    expect(message).not.toBe(describeScheduleSaveFailure(404, { detail: "site not found" }));
  });

  it("translates the backend's 503 auth-unavailable detail distinctly from auth-required", () => {
    const message = describeScheduleSaveFailure(503, { detail: "operator authentication unavailable" });
    expect(message).toContain("인증");
    expect(message).not.toBe(describeScheduleSaveFailure(401, { detail: "operator authentication required" }));
  });

  it("translates the backend's 422 invalid-schedule-configuration detail", () => {
    const message = describeScheduleSaveFailure(422, { detail: "invalid schedule configuration" });
    expect(message).not.toBe("invalid schedule configuration");
    expect(message).toContain("설정");
  });

  it("falls back to a status-coded message for an empty body, distinct from a network failure", () => {
    const message = describeScheduleSaveFailure(502, {});
    expect(message).toContain("502");
    expect(message).not.toBe(SCHEDULE_BACKEND_UNREACHABLE_MESSAGE);
    expect(message).not.toBe(SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE);
  });

  it("falls back to a status-coded message when detail is present but not a usable string", () => {
    const message = describeScheduleSaveFailure(500, { detail: { unexpected: true } });
    expect(message).toContain("500");
  });

  it("does not collapse a nonexistent site, an out-of-range value, and a down backend into one sentence", () => {
    const nonexistentSite = describeScheduleSaveFailure(404, { detail: "site not found" });
    const outOfRange = describeScheduleSaveFailure(400, { detail: "최대 저장 건수는 1000건을 넘을 수 없습니다." });
    const backendDown = SCHEDULE_BACKEND_UNREACHABLE_MESSAGE;
    const distinct = new Set([nonexistentSite, outOfRange, backendDown]);
    expect(distinct.size).toBe(3);
  });
});

describe("distinct failure-mode constants", () => {
  it("gives a client-side network failure a message distinct from a reachable-but-down backend", () => {
    expect(SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE).not.toBe(SCHEDULE_BACKEND_UNREACHABLE_MESSAGE);
  });
});

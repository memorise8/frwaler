import { describe, expect, it } from "vitest";
import { describeJobSubmitFailure } from "../src/lib/job-submit-error";

describe("describeJobSubmitFailure", () => {
  it("translates the known 404 site-not-found detail into actionable Korean", () => {
    const message = describeJobSubmitFailure(404, { detail: "site not found" });
    expect(message).toContain("크롤러");
    expect(message).not.toBe("site not found");
  });

  it("translates the known 409 active-job detail into actionable Korean", () => {
    const message = describeJobSubmitFailure(409, { detail: "site already has an active job" });
    expect(message).toContain("작업");
    expect(message).not.toBe("site already has an active job");
  });

  it("does not collapse a real Pydantic validation array into a generic status-code message", () => {
    // Exact shape observed from a live 422: POST /jobs with limit_n=5000.
    const body = {
      detail: [
        {
          type: "less_than_equal",
          loc: ["body", "limit_n"],
          msg: "Input should be less than or equal to 1000",
          input: 5000,
          ctx: { le: 1000 },
        },
      ],
    };
    const message = describeJobSubmitFailure(422, body);
    expect(message).not.toBe("BE 응답 오류 (422)");
    expect(message).toContain("limit_n");
    expect(message).toContain("less than or equal to 1000");
  });

  it("joins multiple field errors instead of only reporting one", () => {
    const body = {
      detail: [
        { loc: ["body", "mode"], msg: "Field required" },
        { loc: ["body", "limit_n"], msg: "Input should be a valid integer" },
      ],
    };
    const message = describeJobSubmitFailure(422, body);
    expect(message).toContain("mode");
    expect(message).toContain("limit_n");
  });

  it("falls back to a status-coded message for an unrecognized string detail", () => {
    const message = describeJobSubmitFailure(500, { detail: "unexpected server condition" });
    expect(message).toContain("unexpected server condition");
    expect(message).toContain("500");
  });

  it("falls back to a status-coded message when there is no usable detail at all", () => {
    const message = describeJobSubmitFailure(502, {});
    expect(message).toContain("502");
  });

  it("does not throw on a malformed detail array entry", () => {
    const message = describeJobSubmitFailure(422, { detail: [null, "not an object", 42] });
    expect(message).toContain("422");
  });
});

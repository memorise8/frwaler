import { describe, expect, it } from "vitest";
import { classifySafetyState } from "../src/lib/circuit-safety";

describe("classifySafetyState", () => {
  it("is unmeasured when nothing has ever been sampled, even though open is false", () => {
    expect(classifySafetyState({ open: false, recent_sample: 0, baseline_sample: 0 })).toBe("unmeasured");
  });

  it("is unmeasured even if the payload somehow reports open true with no samples", () => {
    expect(classifySafetyState({ open: true, recent_sample: 0, baseline_sample: 0 })).toBe("unmeasured");
  });

  it("is closed once there is a recent and baseline sample and no reasons fired", () => {
    expect(classifySafetyState({ open: false, recent_sample: 50, baseline_sample: 400 })).toBe("closed");
  });

  it("is open when criteria were actually breached", () => {
    expect(classifySafetyState({ open: true, recent_sample: 50, baseline_sample: 400 })).toBe("open");
  });

  it("is measured (not unmeasured) once only one of the two sample counts is non-zero", () => {
    expect(classifySafetyState({ open: false, recent_sample: 12, baseline_sample: 0 })).toBe("closed");
    expect(classifySafetyState({ open: false, recent_sample: 0, baseline_sample: 12 })).toBe("closed");
  });
});

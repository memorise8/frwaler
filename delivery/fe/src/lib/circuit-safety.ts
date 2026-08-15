export type SafetyState = "open" | "closed" | "unmeasured";

export type SafetySample = Readonly<{
  open: boolean;
  recent_sample: number;
  baseline_sample: number;
}>;

// evaluate_circuit (delivery/translation/safety.py:13-71) only appends a
// reason code -- and therefore only sets open=true -- when a threshold is
// actually breached. With zero attempts ever recorded, recent_sample and
// baseline_sample are both 0, failure_rate/baseline_failure_rate are both
// null, no threshold is evaluated, and open stays false by default. That is
// not the same claim as "criteria were checked and passed" -- render it as
// its own state instead of collapsing it into "closed".
export const classifySafetyState = (safety: SafetySample): SafetyState => {
  if (safety.recent_sample === 0 && safety.baseline_sample === 0) return "unmeasured";
  return safety.open ? "open" : "closed";
};

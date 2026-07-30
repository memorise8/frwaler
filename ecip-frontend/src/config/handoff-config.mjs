const configuredFutureHandoffUrl = "";

const toHandoffConfiguration = (candidate) => {
  if (candidate.trim().length === 0) {
    return Object.freeze({ kind: "absent" });
  }

  const parsed = new URL(candidate);
  if (parsed.protocol !== "https:") {
    return Object.freeze({ kind: "invalid" });
  }

  return Object.freeze({ kind: "ready", url: parsed.toString() });
};

export const getHandoffConfiguration = () =>
  toHandoffConfiguration(configuredFutureHandoffUrl);

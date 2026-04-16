"use client";

type Severity = "info" | "warning" | "critical";

interface RiskFlag {
  code: string;
  message: string;
  severity: Severity;
}

interface RiskFlagsProps {
  flags: RiskFlag[];
}

const severityConfig: Record<
  Severity,
  { icon: string; color: string; bg: string; border: string; label: string }
> = {
  info: {
    icon: "ℹ️",
    color: "var(--accent-cyan)",
    bg: "rgba(0,200,240,0.06)",
    border: "rgba(0,200,240,0.2)",
    label: "정보",
  },
  warning: {
    icon: "⚠️",
    color: "var(--accent-amber)",
    bg: "rgba(245,158,11,0.07)",
    border: "rgba(245,158,11,0.25)",
    label: "경고",
  },
  critical: {
    icon: "❌",
    color: "var(--accent-red)",
    bg: "rgba(239,68,68,0.07)",
    border: "rgba(239,68,68,0.25)",
    label: "위험",
  },
};

export default function RiskFlags({ flags }: RiskFlagsProps) {
  if (flags.length === 0) {
    return (
      <div
        className="card"
        style={{
          padding: "1.25rem 1.5rem",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          color: "var(--accent-green)",
        }}
      >
        <span style={{ fontSize: "1.2rem" }}>✅</span>
        <span style={{ fontSize: "0.875rem", fontWeight: 500 }}>
          위험 플래그 없음 — 검토된 파라미터 범위 내 정상
        </span>
      </div>
    );
  }

  // Sort: critical → warning → info
  const order: Severity[] = ["critical", "warning", "info"];
  const sorted = [...flags].sort(
    (a, b) => order.indexOf(a.severity) - order.indexOf(b.severity)
  );

  return (
    <ul style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      {sorted.map((flag, i) => {
        const cfg = severityConfig[flag.severity];
        return (
          <li
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: "0.75rem",
              background: cfg.bg,
              border: `1px solid ${cfg.border}`,
              borderRadius: "0.625rem",
              padding: "0.875rem 1rem",
            }}
          >
            <span style={{ fontSize: "1rem", flexShrink: 0, marginTop: "1px" }}>
              {cfg.icon}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  marginBottom: "0.2rem",
                }}
              >
                <span
                  style={{
                    fontSize: "0.7rem",
                    fontWeight: 600,
                    color: cfg.color,
                    fontFamily: "'DM Mono', monospace",
                    letterSpacing: "0.06em",
                    textTransform: "uppercase",
                  }}
                >
                  {flag.code}
                </span>
                <span
                  className="chip"
                  style={{
                    fontSize: "0.65rem",
                    background: cfg.bg,
                    borderColor: cfg.border,
                    color: cfg.color,
                    padding: "0.1rem 0.45rem",
                  }}
                >
                  {cfg.label}
                </span>
              </div>
              <p
                style={{
                  fontSize: "0.825rem",
                  color: "var(--text-secondary)",
                  margin: 0,
                  lineHeight: 1.5,
                }}
              >
                {flag.message}
              </p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

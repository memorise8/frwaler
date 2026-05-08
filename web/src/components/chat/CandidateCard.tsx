"use client";
import { useState } from "react";

export type Candidate = {
  source: "products" | "heritage";
  mpn: string;
  manufacturer?: string;
  device_type?: string;
  specs: Record<string, unknown>;
  datasheet_url?: string;
  score: number;
  score_breakdown?: Record<string, number>;
  explanation?: string;
};

const PRIORITY_SPEC_KEYS = [
  "Vceo", "Vce", "Ic", "Ib", "hFE", "hfe", "Tj", "Pd",
  "Vds", "Vgs", "Id", "Rds_on", "Vth",
  "R", "C", "L", "V", "I", "tol", "temp_range",
];

function pickTopSpecs(specs: Record<string, unknown>): [string, unknown][] {
  const entries = Object.entries(specs).filter(([, v]) => v !== null && v !== undefined && v !== "");
  // Sort priority keys first
  const sorted = entries.sort(([a], [b]) => {
    const ai = PRIORITY_SPEC_KEYS.findIndex((k) => a.toLowerCase().includes(k.toLowerCase()));
    const bi = PRIORITY_SPEC_KEYS.findIndex((k) => b.toLowerCase().includes(k.toLowerCase()));
    if (ai === -1 && bi === -1) return 0;
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
  return sorted.slice(0, 6);
}

function scoreColor(score: number): string {
  if (score >= 80) return "var(--accent-green)";
  if (score >= 60) return "var(--accent-amber)";
  return "var(--accent-red)";
}

export default function CandidateCard({ c }: { c: Candidate }) {
  const [expanded, setExpanded] = useState(false);
  const topSpecs = pickTopSpecs(c.specs);
  const hasBreakdown = c.score_breakdown && Object.keys(c.score_breakdown).length > 0;

  return (
    <div
      className="card"
      style={{
        padding: "1rem 1.125rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.625rem",
        transition: "border-color 0.2s, box-shadow 0.2s",
      }}
    >
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "0.5rem" }}>
        <div style={{ minWidth: 0 }}>
          <p
            style={{
              fontFamily: "'DM Mono', monospace",
              fontWeight: 700,
              fontSize: "0.95rem",
              color: "var(--text-primary)",
              letterSpacing: "0.02em",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {c.mpn}
          </p>
          {c.manufacturer && (
            <p style={{ fontSize: "0.72rem", color: "var(--text-dim)", marginTop: "0.1rem" }}>
              {c.manufacturer}
            </p>
          )}
        </div>

        {/* Score badge */}
        <div
          style={{
            flexShrink: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "0.15rem",
          }}
        >
          <span
            style={{
              fontFamily: "'DM Mono', monospace",
              fontWeight: 700,
              fontSize: "1.1rem",
              color: scoreColor(c.score),
              lineHeight: 1,
            }}
          >
            {c.score}
          </span>
          <span style={{ fontSize: "0.6rem", color: "var(--text-dim)", letterSpacing: "0.05em" }}>
            SCORE
          </span>
        </div>
      </div>

      {/* Badges row */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            padding: "0.15rem 0.5rem",
            borderRadius: "999px",
            fontSize: "0.65rem",
            fontWeight: 600,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            fontFamily: "'DM Mono', monospace",
            background: c.source === "heritage" ? "rgba(16,185,129,0.1)" : "rgba(0,200,240,0.08)",
            border: c.source === "heritage" ? "1px solid rgba(16,185,129,0.3)" : "1px solid rgba(0,200,240,0.2)",
            color: c.source === "heritage" ? "var(--accent-green)" : "var(--accent-cyan)",
          }}
        >
          {c.source === "heritage" ? "★ HERITAGE" : "DB"}
        </span>
        {c.device_type && (
          <span className="chip chip-neutral" style={{ fontSize: "0.65rem", textTransform: "uppercase" }}>
            {c.device_type}
          </span>
        )}
      </div>

      {/* Specs table */}
      {topSpecs.length > 0 && (
        <div
          style={{
            background: "var(--bg-panel)",
            borderRadius: "0.375rem",
            overflow: "hidden",
            fontSize: "0.72rem",
          }}
        >
          {topSpecs.map(([key, val], i) => (
            <div
              key={key}
              className="spec-row"
              style={{
                display: "flex",
                justifyContent: "space-between",
                padding: "0.25rem 0.5rem",
                gap: "0.5rem",
              }}
            >
              <span style={{ color: "var(--text-dim)", fontFamily: "'DM Mono', monospace", flexShrink: 0 }}>
                {key}
              </span>
              <span
                style={{
                  color: "var(--text-secondary)",
                  fontFamily: "'DM Mono', monospace",
                  textAlign: "right",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {String(val)}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Explanation */}
      {c.explanation && (
        <p style={{ fontSize: "0.75rem", color: "var(--text-secondary)", lineHeight: 1.5, margin: 0 }}>
          {c.explanation}
        </p>
      )}

      {/* Footer row */}
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginTop: "0.125rem" }}>
        {c.datasheet_url && (
          <a
            href={c.datasheet_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              fontSize: "0.72rem",
              color: "var(--accent-cyan)",
              textDecoration: "none",
              fontFamily: "'DM Mono', monospace",
              display: "inline-flex",
              alignItems: "center",
              gap: "0.25rem",
            }}
            onMouseOver={(e) => ((e.currentTarget as HTMLElement).style.opacity = "0.75")}
            onMouseOut={(e) => ((e.currentTarget as HTMLElement).style.opacity = "1")}
          >
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none" style={{ flexShrink: 0 }}>
              <path d="M2 10L10 2M10 2H5M10 2V7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Datasheet
          </a>
        )}

        {hasBreakdown && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            style={{
              background: "transparent",
              border: "none",
              padding: 0,
              cursor: "pointer",
              fontSize: "0.72rem",
              color: "var(--text-dim)",
              fontFamily: "'DM Mono', monospace",
              display: "inline-flex",
              alignItems: "center",
              gap: "0.2rem",
              transition: "color 0.15s",
            }}
            onMouseOver={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--text-secondary)")}
            onMouseOut={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--text-dim)")}
          >
            {expanded ? "▲" : "▼"} 점수 상세
          </button>
        )}
      </div>

      {/* Score breakdown (expanded) */}
      {expanded && hasBreakdown && (
        <div
          style={{
            background: "rgba(0,0,0,0.2)",
            borderRadius: "0.375rem",
            padding: "0.625rem 0.75rem",
            display: "flex",
            flexDirection: "column",
            gap: "0.3rem",
          }}
        >
          {Object.entries(c.score_breakdown!).map(([factor, val]) => (
            <div
              key={factor}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                fontSize: "0.68rem",
                gap: "0.5rem",
              }}
            >
              <span style={{ color: "var(--text-dim)", fontFamily: "'DM Mono', monospace" }}>
                {factor}
              </span>
              <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                <div
                  style={{
                    width: "60px",
                    height: "4px",
                    background: "var(--border-dim)",
                    borderRadius: "2px",
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      width: `${Math.min(100, Math.max(0, (val / 10) * 100))}%`,
                      height: "100%",
                      background: scoreColor(val * 10),
                      borderRadius: "2px",
                      transition: "width 0.3s ease",
                    }}
                  />
                </div>
                <span style={{ color: "var(--text-secondary)", fontFamily: "'DM Mono', monospace", minWidth: "2rem", textAlign: "right" }}>
                  {typeof val === "number" ? val.toFixed(1) : val}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

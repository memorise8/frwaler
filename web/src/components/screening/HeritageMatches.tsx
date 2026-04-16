"use client";

interface HeritageMatch {
  mpn: string;
  manufacturer?: string;
  qual_level?: string;
  similarity: number;
  source_url?: string;
}

interface HeritageMatchesProps {
  matches: HeritageMatch[];
}

function QualBadge({ level }: { level?: string }) {
  if (!level) return null;

  const colors: Record<string, { color: string; bg: string; border: string }> =
    {
      JANS: {
        color: "#10b981",
        bg: "rgba(16,185,129,0.1)",
        border: "rgba(16,185,129,0.3)",
      },
      JANTX: {
        color: "#10b981",
        bg: "rgba(16,185,129,0.12)",
        border: "rgba(16,185,129,0.35)",
      },
      JANTXV: {
        color: "#00c8f0",
        bg: "rgba(0,200,240,0.1)",
        border: "rgba(0,200,240,0.3)",
      },
      "MIL-SPEC": {
        color: "#f59e0b",
        bg: "rgba(245,158,11,0.1)",
        border: "rgba(245,158,11,0.3)",
      },
      "Space-Grade": {
        color: "#00c8f0",
        bg: "rgba(0,200,240,0.12)",
        border: "rgba(0,200,240,0.35)",
      },
    };

  const style = colors[level] || {
    color: "var(--text-secondary)",
    bg: "rgba(255,255,255,0.04)",
    border: "var(--border-dim)",
  };

  return (
    <span
      style={{
        display: "inline-block",
        fontSize: "0.68rem",
        fontWeight: 600,
        fontFamily: "'DM Mono', monospace",
        letterSpacing: "0.04em",
        padding: "0.15rem 0.5rem",
        borderRadius: "999px",
        background: style.bg,
        border: `1px solid ${style.border}`,
        color: style.color,
      }}
    >
      {level}
    </span>
  );
}

export default function HeritageMatches({ matches }: HeritageMatchesProps) {
  const top = matches.slice(0, 3);

  if (top.length === 0) {
    return (
      <div
        style={{
          color: "var(--text-dim)",
          fontSize: "0.875rem",
          padding: "1rem 0",
        }}
      >
        헤리티지 매칭 데이터 없음
      </div>
    );
  }

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${top.length}, 1fr)`,
        gap: "1rem",
      }}
      className="max-sm:grid-cols-1"
    >
      {top.map((match, i) => {
        const pct = Math.round(match.similarity * 100);
        const barColor =
          pct >= 80 ? "#10b981" : pct >= 60 ? "#f59e0b" : "#8fa3c0";

        return (
          <div
            key={i}
            className="card"
            style={{
              padding: "1.25rem",
              display: "flex",
              flexDirection: "column",
              gap: "0.875rem",
            }}
          >
            {/* Rank badge */}
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                justifyContent: "space-between",
              }}
            >
              <span
                style={{
                  fontSize: "0.65rem",
                  fontFamily: "'DM Mono', monospace",
                  color: "var(--text-dim)",
                  letterSpacing: "0.08em",
                }}
              >
                #{i + 1} 매치
              </span>
              <QualBadge level={match.qual_level} />
            </div>

            {/* MPN */}
            <div>
              <div
                style={{
                  fontFamily: "'DM Mono', monospace",
                  fontWeight: 600,
                  fontSize: "1rem",
                  color: "var(--text-primary)",
                  letterSpacing: "0.02em",
                  marginBottom: "0.2rem",
                }}
              >
                {match.mpn}
              </div>
              {match.manufacturer && (
                <div
                  style={{
                    fontSize: "0.775rem",
                    color: "var(--text-secondary)",
                  }}
                >
                  {match.manufacturer}
                </div>
              )}
            </div>

            {/* Similarity bar */}
            <div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  marginBottom: "0.35rem",
                }}
              >
                <span
                  style={{
                    fontSize: "0.72rem",
                    color: "var(--text-dim)",
                  }}
                >
                  유사도
                </span>
                <span
                  style={{
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    fontFamily: "'DM Mono', monospace",
                    color: barColor,
                  }}
                >
                  {pct}%
                </span>
              </div>
              <div
                style={{
                  height: "5px",
                  background: "rgba(255,255,255,0.06)",
                  borderRadius: "3px",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    width: `${pct}%`,
                    background: barColor,
                    borderRadius: "3px",
                    boxShadow: `0 0 6px ${barColor}60`,
                    transition: "width 0.6s ease",
                  }}
                />
              </div>
            </div>

            {/* Source link */}
            {match.source_url && (
              <a
                href={match.source_url}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  fontSize: "0.72rem",
                  color: "var(--accent-cyan)",
                  textDecoration: "none",
                  display: "flex",
                  alignItems: "center",
                  gap: "0.3rem",
                }}
              >
                <svg
                  width="10"
                  height="10"
                  viewBox="0 0 10 10"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                >
                  <path d="M1 5h8M5 1l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                출처 보기
              </a>
            )}
          </div>
        );
      })}
    </div>
  );
}

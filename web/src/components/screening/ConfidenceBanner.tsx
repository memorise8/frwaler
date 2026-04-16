"use client";

interface ConfidenceBannerProps {
  confidence: number; // 0..1
}

export default function ConfidenceBanner({ confidence }: ConfidenceBannerProps) {
  const pct = Math.round(confidence * 100);

  // Color intensity scales with inverse confidence (low confidence = more alarming)
  const alpha = 0.04 + (1 - confidence) * 0.12;
  const borderAlpha = 0.15 + (1 - confidence) * 0.35;

  return (
    <div
      style={{
        position: "sticky",
        top: "56px", // below NavBar
        zIndex: 40,
        background: `rgba(245,158,11,${alpha})`,
        border: `1px solid rgba(245,158,11,${borderAlpha})`,
        backdropFilter: "blur(8px)",
        WebkitBackdropFilter: "blur(8px)",
        borderRadius: "0.625rem",
        padding: "0.7rem 1.1rem",
        display: "flex",
        alignItems: "flex-start",
        gap: "0.625rem",
      }}
      role="alert"
    >
      <span style={{ fontSize: "0.95rem", flexShrink: 0, marginTop: "1px" }}>
        ⚠️
      </span>
      <p
        style={{
          margin: 0,
          fontSize: "0.78rem",
          color: "var(--text-secondary)",
          lineHeight: 1.6,
        }}
      >
        이 결과는 데이터시트 기반 상한 추정치입니다. 공정(fab) 정보가 반영되지
        않으므로, 실제 업스크리닝 시험 통과 가능성은 반드시{" "}
        <strong style={{ color: "var(--text-primary)" }}>
          하드웨어 검증
        </strong>
        으로 확인해야 합니다.{" "}
        <span
          style={{
            fontFamily: "'DM Mono', monospace",
            color: pct >= 80 ? "#10b981" : pct >= 60 ? "#f59e0b" : "#ef4444",
            fontWeight: 600,
          }}
        >
          Confidence: {pct}%
        </span>
      </p>
    </div>
  );
}

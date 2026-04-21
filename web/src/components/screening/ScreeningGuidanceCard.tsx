"use client";
import type { ScreeningGuidance } from "@/lib/api";

type Props = {
  guidance: ScreeningGuidance;
  onUploadClick?: () => void;
};

const PALETTE: Record<string, { bg: string; border: string; color: string; icon: string }> = {
  needs_upload: {
    bg: "rgba(14,165,233,0.08)",
    border: "rgba(14,165,233,0.3)",
    color: "#38bdf8",
    icon: "📄",
  },
  not_found: {
    bg: "rgba(139,92,246,0.08)",
    border: "rgba(139,92,246,0.3)",
    color: "#a78bfa",
    icon: "🔍",
  },
  fetch_failed: {
    bg: "rgba(245,158,11,0.08)",
    border: "rgba(245,158,11,0.3)",
    color: "#fbbf24",
    icon: "⚠️",
  },
};

export default function ScreeningGuidanceCard({ guidance, onUploadClick }: Props) {
  const palette = PALETTE[guidance.error_code] || PALETTE.needs_upload;

  const title =
    guidance.error_code === "needs_upload"
      ? "데이터시트가 필요합니다"
      : guidance.error_code === "not_found"
      ? "아직 등록되지 않은 부품"
      : guidance.error_code === "fetch_failed"
      ? "자동 다운로드 실패"
      : "분석 추가 정보 필요";

  const steps: Array<{ label: string; action?: () => void; href?: string; secondary?: string }> = [];

  if (guidance.datasheet_url) {
    steps.push({
      label: "데이터시트 원본 열기",
      href: guidance.datasheet_url,
      secondary: "벤더 공식 PDF",
    });
  }
  if (guidance.product_url && !guidance.datasheet_url) {
    steps.push({
      label: "제품 페이지 열기",
      href: guidance.product_url,
      secondary: guidance.brand ? `${guidance.brand} 공식` : "벤더 공식",
    });
  }
  steps.push({
    label: "받은 PDF를 업로드 영역으로 끌어다 놓기",
    action: onUploadClick,
    secondary: "왼쪽 '데이터시트 업로드' 패널",
  });

  return (
    <div
      style={{
        background: palette.bg,
        border: `1px solid ${palette.border}`,
        borderRadius: "0.625rem",
        padding: "1rem 1.125rem",
        marginBottom: "1rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.75rem",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
        <span style={{ fontSize: "1.1rem" }}>{palette.icon}</span>
        <div style={{ flex: 1 }}>
          <p
            style={{
              fontSize: "0.82rem",
              fontWeight: 600,
              color: palette.color,
              margin: 0,
              letterSpacing: "-0.01em",
            }}
          >
            {title}
            {guidance.mpn && (
              <span
                style={{
                  color: "var(--text-dim)",
                  fontFamily: "'DM Mono', monospace",
                  marginLeft: "0.5rem",
                  fontSize: "0.78rem",
                  fontWeight: 500,
                }}
              >
                ({guidance.mpn})
              </span>
            )}
          </p>
          <p
            style={{
              fontSize: "0.78rem",
              color: "var(--text-secondary)",
              margin: "0.2rem 0 0 0",
              lineHeight: 1.5,
            }}
          >
            {guidance.message}
          </p>
        </div>
      </div>

      <ol
        style={{
          listStyle: "none",
          padding: 0,
          margin: 0,
          display: "flex",
          flexDirection: "column",
          gap: "0.4rem",
        }}
      >
        {steps.map((step, i) => (
          <li
            key={i}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.6rem",
              fontSize: "0.78rem",
              color: "var(--text-secondary)",
            }}
          >
            <span
              style={{
                width: "20px",
                height: "20px",
                borderRadius: "50%",
                background: `${palette.color}22`,
                color: palette.color,
                fontSize: "0.7rem",
                fontWeight: 700,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              {i + 1}
            </span>
            {step.href ? (
              <a
                href={step.href}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  color: palette.color,
                  textDecoration: "none",
                  borderBottom: `1px dashed ${palette.color}66`,
                  paddingBottom: "1px",
                }}
              >
                {step.label}
                <span style={{ fontSize: "0.85em", marginLeft: "0.3rem" }}>↗</span>
              </a>
            ) : step.action ? (
              <button
                onClick={step.action}
                style={{
                  background: "transparent",
                  border: "none",
                  color: palette.color,
                  cursor: "pointer",
                  padding: 0,
                  fontSize: "inherit",
                  textAlign: "left",
                  borderBottom: `1px dashed ${palette.color}66`,
                  paddingBottom: "1px",
                }}
              >
                {step.label} ↓
              </button>
            ) : (
              <span>{step.label}</span>
            )}
            {step.secondary && (
              <span
                style={{
                  fontSize: "0.68rem",
                  color: "var(--text-dim)",
                  marginLeft: "auto",
                  fontFamily: "'DM Mono', monospace",
                  letterSpacing: "0.03em",
                }}
              >
                {step.secondary}
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

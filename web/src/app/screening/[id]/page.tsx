"use client";
import { use } from "react";
import useSWR from "swr";
import { getScreening } from "@/lib/api";
import ScoreGauge from "@/components/screening/ScoreGauge";
import ConfidenceBanner from "@/components/screening/ConfidenceBanner";
import FactorTable from "@/components/screening/FactorTable";
import HeritageMatches from "@/components/screening/HeritageMatches";
import RiskFlags from "@/components/screening/RiskFlags";

// ─── Types ────────────────────────────────────────────────────────────────────

type FactorSource = { title: string; url: string; doi?: string; note?: string };
type FactorScore = {
  name: string;
  score: number;
  weight: number;
  coverage: number;
  rationale: string;
  sources: FactorSource[];
  value: number | null;
  direction: string;
};
type HeritageMatch = {
  mpn: string;
  manufacturer?: string;
  qual_level?: string;
  similarity: number;
  source_url?: string;
};
type RiskFlag = {
  code: string;
  message: string;
  severity: "info" | "warning" | "critical";
};
type BjtParameters = {
  vceo_v: number | null;
  vcbo_v: number | null;
  vebo_v: number | null;
  ic_max_a: number | null;
  hfe_min: number | null;
  hfe_max: number | null;
  icbo_a_at_vcb: number | null;
  ft_hz: number | null;
  pd_w: number | null;
  tj_max_c: number | null;
  polarity: string | null;
  package: string | null;
};
type ScreeningReport = {
  id: string;
  input_mpn?: string;
  input_source: "pdf" | "mpn";
  parameters: BjtParameters;
  factor_scores: FactorScore[];
  overall_score: number;
  status: "pass" | "caution" | "fail";
  confidence: number;
  heritage_matches: HeritageMatch[];
  risk_flags: RiskFlag[];
  extraction_confidence?: number;
  tokens_used: number;
  created_at?: string;
};

// ─── Param display helper ─────────────────────────────────────────────────────

const PARAM_LABELS: Record<string, string> = {
  vceo_v: "VCEO (V)",
  vcbo_v: "VCBO (V)",
  vebo_v: "VEBO (V)",
  ic_max_a: "Ic_max (A)",
  hfe_min: "hFE min",
  hfe_max: "hFE max",
  icbo_a_at_vcb: "ICBO (A)",
  ft_hz: "ft (Hz)",
  pd_w: "Pd (W)",
  tj_max_c: "Tj_max (°C)",
  polarity: "극성",
  package: "패키지",
};

function formatParamValue(key: string, val: number | string | null): string {
  if (val === null) return "—";
  if (key === "ft_hz" && typeof val === "number") {
    if (val >= 1e9) return `${(val / 1e9).toFixed(1)} GHz`;
    if (val >= 1e6) return `${(val / 1e6).toFixed(1)} MHz`;
    return `${val} Hz`;
  }
  if (key === "icbo_a_at_vcb" && typeof val === "number") {
    if (val < 1e-6) return `${(val * 1e9).toFixed(1)} nA`;
    if (val < 1e-3) return `${(val * 1e6).toFixed(1)} μA`;
    return `${val} A`;
  }
  return String(val);
}

// ─── Status helpers ───────────────────────────────────────────────────────────

function statusInfo(status: ScreeningReport["status"]) {
  if (status === "pass")
    return {
      emoji: "✅",
      label: "사용 가능",
      color: "#10b981",
      bg: "rgba(16,185,129,0.08)",
      border: "rgba(16,185,129,0.25)",
    };
  if (status === "caution")
    return {
      emoji: "⚠️",
      label: "주의",
      color: "#f59e0b",
      bg: "rgba(245,158,11,0.08)",
      border: "rgba(245,158,11,0.25)",
    };
  return {
    emoji: "❌",
    label: "부적합",
    color: "#ef4444",
    bg: "rgba(239,68,68,0.08)",
    border: "rgba(239,68,68,0.25)",
  };
}

// ─── Section wrapper ──────────────────────────────────────────────────────────

function Section({
  title,
  children,
  delay = 0,
}: {
  title: string;
  children: React.ReactNode;
  delay?: number;
}) {
  return (
    <section
      className={`animate-fade-up animate-fade-up-${delay}`}
      style={{ marginBottom: "2.5rem" }}
    >
      <h2
        style={{
          fontSize: "0.775rem",
          fontWeight: 600,
          color: "var(--text-dim)",
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          marginBottom: "0.875rem",
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
        }}
      >
        <span
          style={{
            display: "inline-block",
            width: "16px",
            height: "1px",
            background: "var(--accent-cyan)",
            opacity: 0.6,
          }}
        />
        {title}
      </h2>
      {children}
    </section>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

interface PageProps {
  params: Promise<{ id: string }>;
}

function getLicenseKey(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("bjt_license_key") || "";
}

export default function ScreeningResultPage({ params }: PageProps) {
  const { id } = use(params);
  const licenseKey = typeof window !== "undefined" ? getLicenseKey() : "";

  const { data: report, error, isLoading } = useSWR<ScreeningReport>(
    id ? ["screen-bjt", id] : null,
    () => getScreening(id, licenseKey),
    { revalidateOnFocus: false }
  );

  // ── Loading ──
  if (isLoading) {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "50vh",
          gap: "1.25rem",
        }}
      >
        <div
          style={{
            width: "48px",
            height: "48px",
            borderRadius: "50%",
            border: "2.5px solid var(--border-dim)",
            borderTopColor: "var(--accent-cyan)",
            animation: "spin 0.9s linear infinite",
          }}
        />
        <p style={{ color: "var(--text-dim)", fontSize: "0.875rem" }}>
          분석 결과 불러오는 중...
        </p>
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  // ── Error ──
  if (error || !report) {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "40vh",
          gap: "0.875rem",
        }}
      >
        <span style={{ fontSize: "2rem" }}>❌</span>
        <p style={{ color: "var(--accent-red)", fontWeight: 600 }}>
          결과를 불러오지 못했습니다
        </p>
        <p style={{ color: "var(--text-dim)", fontSize: "0.8rem" }}>
          {error?.message || "알 수 없는 오류"}
        </p>
        <a href="/screening" className="btn-ghost" style={{ marginTop: "0.5rem" }}>
          ← 다시 시도
        </a>
      </div>
    );
  }

  const si = statusInfo(report.status);
  const paramEntries = Object.entries(PARAM_LABELS) as [string, string][];

  return (
    <div className="animate-fade-up">
      {/* ── Breadcrumb ── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          fontSize: "0.775rem",
          color: "var(--text-dim)",
          marginBottom: "1.5rem",
        }}
      >
        <a
          href="/screening"
          style={{ color: "var(--text-dim)", textDecoration: "none" }}
          onMouseEnter={(e) => (e.currentTarget.style.color = "var(--accent-cyan)")}
          onMouseLeave={(e) => (e.currentTarget.style.color = "var(--text-dim)")}
        >
          업스크리닝
        </a>
        <span>/</span>
        <span
          style={{
            fontFamily: "'DM Mono', monospace",
            color: "var(--text-secondary)",
          }}
        >
          {report.input_mpn || id.slice(0, 8)}
        </span>
      </div>

      {/* ── Hero row: Gauge + Status + Parameters ── */}
      <Section title="종합 평가" delay={1}>
        <div
          className="card stat-card"
          style={{
            padding: "2rem",
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            gap: "2.5rem",
            alignItems: "center",
            background: si.bg,
            borderColor: si.border,
          }}
        >
          {/* Gauge */}
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.5rem" }}>
            <ScoreGauge score={report.overall_score} size={180} />
            <div
              style={{
                fontFamily: "'DM Mono', monospace",
                fontSize: "0.68rem",
                color: "var(--text-dim)",
                letterSpacing: "0.06em",
              }}
            >
              ID: {id.slice(0, 12)}...
            </div>
          </div>

          {/* Info panel */}
          <div>
            {/* MPN + status chip */}
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                flexWrap: "wrap",
                gap: "0.75rem",
                marginBottom: "1rem",
              }}
            >
              {report.input_mpn && (
                <h1
                  style={{
                    fontFamily: "'DM Mono', monospace",
                    fontSize: "1.5rem",
                    fontWeight: 700,
                    color: "var(--text-primary)",
                    letterSpacing: "0.02em",
                    margin: 0,
                  }}
                >
                  {report.input_mpn}
                </h1>
              )}
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.4rem",
                  padding: "0.3rem 0.8rem",
                  borderRadius: "999px",
                  background: si.bg,
                  border: `1px solid ${si.border}`,
                  color: si.color,
                  fontWeight: 600,
                  fontSize: "0.825rem",
                }}
              >
                {si.emoji} {si.label}
              </span>
              <span
                className="chip chip-neutral"
                style={{ fontSize: "0.7rem" }}
              >
                {report.input_source === "pdf"
                  ? "데이터시트 분석"
                  : "MPN 조회"}
              </span>
            </div>

            {/* Parameter grid */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
                gap: "0.5rem",
              }}
            >
              {paramEntries.map(([key, label]) => {
                const raw = report.parameters[key as keyof BjtParameters];
                const val = formatParamValue(key, raw);
                if (val === "—") return null;
                return (
                  <div
                    key={key}
                    style={{
                      background: "rgba(255,255,255,0.03)",
                      border: "1px solid var(--border-dim)",
                      borderRadius: "0.5rem",
                      padding: "0.5rem 0.75rem",
                    }}
                  >
                    <div
                      style={{
                        fontSize: "0.65rem",
                        color: "var(--text-dim)",
                        letterSpacing: "0.05em",
                        marginBottom: "0.2rem",
                        textTransform: "uppercase",
                      }}
                    >
                      {label}
                    </div>
                    <div
                      style={{
                        fontFamily: "'DM Mono', monospace",
                        fontSize: "0.875rem",
                        fontWeight: 600,
                        color: "var(--text-primary)",
                      }}
                    >
                      {val}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Meta row */}
            {report.created_at && (
              <p
                style={{
                  marginTop: "0.875rem",
                  fontSize: "0.7rem",
                  color: "var(--text-dim)",
                  fontFamily: "'DM Mono', monospace",
                }}
              >
                분석 시각:{" "}
                {new Date(report.created_at).toLocaleString("ko-KR")} ·
                토큰 사용: {report.tokens_used.toLocaleString()}
              </p>
            )}
          </div>
        </div>
      </Section>

      {/* ── Confidence banner ── */}
      <div style={{ marginBottom: "2.5rem" }}>
        <ConfidenceBanner confidence={report.confidence} />
      </div>

      {/* ── Factor table ── */}
      <Section title="평가 인자 상세" delay={2}>
        <FactorTable factors={report.factor_scores} />
      </Section>

      {/* ── Heritage matches ── */}
      <Section title="헤리티지 매칭" delay={3}>
        <p
          style={{
            fontSize: "0.78rem",
            color: "var(--text-secondary)",
            marginBottom: "0.875rem",
          }}
        >
          우주 비행 이력이 있는 유사 부품과의 비교 결과입니다. 유사도가 높을수록 신뢰도가 올라갑니다.
        </p>
        <HeritageMatches matches={report.heritage_matches} />
      </Section>

      {/* ── Risk flags ── */}
      <Section title="위험 플래그" delay={4}>
        <p
          style={{
            fontSize: "0.78rem",
            color: "var(--text-secondary)",
            marginBottom: "0.875rem",
          }}
        >
          업스크리닝 시 특별히 주의해야 할 항목입니다.
        </p>
        <RiskFlags flags={report.risk_flags} />
      </Section>

      {/* ── Back link ── */}
      <div style={{ paddingTop: "1rem", paddingBottom: "2rem" }}>
        <a href="/screening" className="btn-ghost" style={{ fontSize: "0.825rem" }}>
          ← 다른 부품 분석하기
        </a>
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

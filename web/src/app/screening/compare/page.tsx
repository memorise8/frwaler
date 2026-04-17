"use client";
import { useState, useCallback } from "react";
import { getScreening, screenBjtByMpn } from "@/lib/api";
import ScoreGauge from "@/components/screening/ScoreGauge";
import HeritageMatches from "@/components/screening/HeritageMatches";
import RiskFlags from "@/components/screening/RiskFlags";
import Tooltip from "@/components/screening/Tooltip";

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
type ScreeningReport = {
  id: string;
  input_mpn?: string;
  input_source: "pdf" | "mpn";
  parameters: Record<string, number | string | null>;
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

// ─── Tooltip content per factor ───────────────────────────────────────────────

const FACTOR_TOOLTIPS: Record<string, string> = {
  icbo: "ICBO는 누설 전류로, 우주 방사선 누적에 따라 가장 먼저 변하는 파라미터입니다.",
  vceo: "VCEO는 이미터 개방 시 컬렉터-베이스 간 최대 허용 전압입니다.",
  vcbo: "VCBO는 이미터 개방 시 컬렉터-베이스 최대 역전압입니다.",
  hfe: "hFE는 전류 이득으로, 방사선 누적 시 감소하는 경향이 있습니다.",
  ft: "ft는 전환 주파수로, 스위칭 속도를 나타냅니다.",
  pd: "최대 전력 소산(Pd)은 열 설계 마진과 직결됩니다.",
  tj: "최대 접합 온도(Tj_max)입니다.",
  package: "패키지 형태는 방사선 차폐 효과와 우주 적합성에 영향을 줍니다.",
  ic: "최대 컬렉터 전류(Ic_max)입니다.",
  heritage: "헤리티지 점수는 유사 부품의 우주 비행 이력을 반영합니다.",
};

function getFactorTooltip(name: string): string {
  const lower = name.toLowerCase();
  for (const [key, tip] of Object.entries(FACTOR_TOOLTIPS)) {
    if (lower.includes(key)) return tip;
  }
  return `${name} 파라미터 — 우주 방사선 내성 평가에 사용되는 지표입니다.`;
}

// ─── Status helpers ───────────────────────────────────────────────────────────

function statusInfo(status: ScreeningReport["status"]) {
  if (status === "pass")
    return { emoji: "✅", label: "사용 가능", color: "#10b981", bg: "rgba(16,185,129,0.08)", border: "rgba(16,185,129,0.25)" };
  if (status === "caution")
    return { emoji: "⚠️", label: "주의", color: "#f59e0b", bg: "rgba(245,158,11,0.08)", border: "rgba(245,158,11,0.25)" };
  return { emoji: "❌", label: "부적합", color: "#ef4444", bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.25)" };
}

function getLicenseKey(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("bjt_license_key") || "";
}

// ─── Mini score bar ───────────────────────────────────────────────────────────

function MiniBar({ score, width = 80 }: { score: number; width?: number }) {
  const color = score >= 0.75 ? "#10b981" : score >= 0.55 ? "#f59e0b" : "#ef4444";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
      <div
        style={{
          width: `${width}px`,
          height: "5px",
          background: "rgba(255,255,255,0.06)",
          borderRadius: "3px",
          overflow: "hidden",
          flexShrink: 0,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${score * 100}%`,
            background: color,
            borderRadius: "3px",
            boxShadow: `0 0 4px ${color}60`,
          }}
        />
      </div>
      <span
        style={{
          fontSize: "0.72rem",
          fontFamily: "'DM Mono', monospace",
          color,
          fontWeight: 600,
          minWidth: "24px",
        }}
      >
        {(score * 100).toFixed(0)}
      </span>
    </div>
  );
}

// ─── Spinner ──────────────────────────────────────────────────────────────────

function Spinner({ label }: { label?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.75rem", padding: "2rem 0" }}>
      <div
        style={{
          width: "36px",
          height: "36px",
          borderRadius: "50%",
          border: "2.5px solid var(--border-dim)",
          borderTopColor: "var(--accent-cyan)",
          animation: "spin 0.9s linear infinite",
        }}
      />
      {label && <p style={{ color: "var(--text-dim)", fontSize: "0.8rem" }}>{label}</p>}
    </div>
  );
}

// ─── Section label ────────────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: "0.7rem",
        fontWeight: 600,
        color: "var(--text-dim)",
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        marginBottom: "1rem",
      }}
    >
      <span
        style={{
          display: "inline-block",
          width: "14px",
          height: "1px",
          background: "var(--accent-cyan)",
          opacity: 0.6,
        }}
      />
      {children}
    </div>
  );
}

// ─── Factor comparison table ──────────────────────────────────────────────────

function FactorCompareTable({
  factorsA,
  factorsB,
  labelA,
  labelB,
}: {
  factorsA: FactorScore[];
  factorsB: FactorScore[];
  labelA: string;
  labelB: string;
}) {
  const [sortByDiff, setSortByDiff] = useState(true);

  // Merge factors by name
  const allNames = Array.from(
    new Set([...factorsA.map((f) => f.name), ...factorsB.map((f) => f.name)])
  );

  type Row = {
    name: string;
    scoreA: number | null;
    scoreB: number | null;
    valueA: number | null;
    valueB: number | null;
    diff: number | null;
  };

  const rows: Row[] = allNames.map((name) => {
    const a = factorsA.find((f) => f.name === name) ?? null;
    const b = factorsB.find((f) => f.name === name) ?? null;
    const diff = a && b ? a.score - b.score : null;
    return {
      name,
      scoreA: a?.score ?? null,
      scoreB: b?.score ?? null,
      valueA: a?.value ?? null,
      valueB: b?.value ?? null,
      diff,
    };
  });

  const sorted = [...rows].sort((ra, rb) => {
    if (!sortByDiff) return 0;
    const da = ra.diff !== null ? Math.abs(ra.diff) : -1;
    const db = rb.diff !== null ? Math.abs(rb.diff) : -1;
    return db - da;
  });

  const thStyle: React.CSSProperties = {
    padding: "0.55rem 0.875rem",
    fontSize: "0.68rem",
    fontWeight: 600,
    color: "var(--text-dim)",
    letterSpacing: "0.07em",
    textTransform: "uppercase",
    borderBottom: "1px solid var(--border-dim)",
    textAlign: "left",
    whiteSpace: "nowrap",
  };

  const tdStyle: React.CSSProperties = {
    padding: "0.7rem 0.875rem",
    fontSize: "0.8rem",
    borderBottom: "1px solid rgba(30,45,74,0.5)",
    verticalAlign: "middle",
  };

  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0.875rem 1rem 0.625rem",
          borderBottom: "1px solid var(--border-dim)",
        }}
      >
        <span style={{ fontSize: "0.75rem", color: "var(--text-secondary)", fontWeight: 500 }}>
          평가 인자 비교
        </span>
        <button
          onClick={() => setSortByDiff((v) => !v)}
          style={{
            background: sortByDiff ? "rgba(0,200,240,0.1)" : "transparent",
            border: `1px solid ${sortByDiff ? "rgba(0,200,240,0.3)" : "var(--border-dim)"}`,
            color: sortByDiff ? "var(--accent-cyan)" : "var(--text-dim)",
            borderRadius: "0.375rem",
            padding: "0.25rem 0.625rem",
            fontSize: "0.68rem",
            cursor: "pointer",
            transition: "all 0.15s",
          }}
        >
          {sortByDiff ? "차이 큰 순" : "기본 순"}
        </button>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "rgba(255,255,255,0.02)" }}>
              <th style={thStyle}>인자</th>
              <th style={{ ...thStyle, color: "var(--accent-cyan)" }}>{labelA} 점수</th>
              <th style={{ ...thStyle, color: "#8fa3c0" }}>{labelB} 점수</th>
              <th style={thStyle}>차이</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const diffAbs = row.diff !== null ? Math.abs(row.diff) : null;
              const aWins = row.diff !== null && row.diff > 0.01;
              const bWins = row.diff !== null && row.diff < -0.01;
              const diffColor = aWins ? "#10b981" : bWins ? "#ef4444" : "var(--text-dim)";
              const diffLabel =
                row.diff === null
                  ? "—"
                  : Math.abs(row.diff) < 0.01
                  ? "동일"
                  : aWins
                  ? `A +${(row.diff * 100).toFixed(0)}`
                  : `B +${(Math.abs(row.diff) * 100).toFixed(0)}`;

              return (
                <tr
                  key={row.name}
                  style={{ transition: "background 0.12s" }}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = "rgba(0,200,240,0.025)"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = ""; }}
                >
                  {/* Factor name */}
                  <td style={{ ...tdStyle, color: "var(--text-primary)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
                      <span style={{ fontFamily: "'DM Mono', monospace", fontWeight: 500, fontSize: "0.8rem" }}>
                        {row.name}
                      </span>
                      <Tooltip text={getFactorTooltip(row.name)}>
                        <span
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            justifyContent: "center",
                            width: "14px",
                            height: "14px",
                            borderRadius: "50%",
                            border: "1px solid var(--border-bright)",
                            color: "var(--text-dim)",
                            fontSize: "0.55rem",
                            cursor: "default",
                            flexShrink: 0,
                          }}
                        >
                          ?
                        </span>
                      </Tooltip>
                    </div>
                  </td>

                  {/* A score */}
                  <td style={tdStyle}>
                    {row.scoreA !== null ? (
                      <MiniBar score={row.scoreA} width={72} />
                    ) : (
                      <span style={{ color: "var(--text-dim)", fontSize: "0.75rem" }}>—</span>
                    )}
                  </td>

                  {/* B score */}
                  <td style={tdStyle}>
                    {row.scoreB !== null ? (
                      <MiniBar score={row.scoreB} width={72} />
                    ) : (
                      <span style={{ color: "var(--text-dim)", fontSize: "0.75rem" }}>—</span>
                    )}
                  </td>

                  {/* Diff */}
                  <td style={tdStyle}>
                    <span
                      style={{
                        fontFamily: "'DM Mono', monospace",
                        fontSize: "0.75rem",
                        fontWeight: 600,
                        color: diffColor,
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "0.25rem",
                      }}
                    >
                      {diffAbs !== null && diffAbs >= 0.01 && (
                        <span
                          style={{
                            width: "6px",
                            height: "6px",
                            borderRadius: "50%",
                            background: diffColor,
                            flexShrink: 0,
                          }}
                        />
                      )}
                      {diffLabel}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Report panel (left or right column) ─────────────────────────────────────

function ReportPanel({
  report,
  label,
  accentColor,
}: {
  report: ScreeningReport;
  label: string;
  accentColor: string;
}) {
  const si = statusInfo(report.status);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {/* Header card */}
      <div
        className="card stat-card"
        style={{
          padding: "1.5rem",
          background: si.bg,
          borderColor: si.border,
          position: "relative",
        }}
      >
        {/* Column label strip */}
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: "2px",
            background: accentColor,
            borderRadius: "0.75rem 0.75rem 0 0",
          }}
        />

        {/* Label tag */}
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.4rem",
            fontSize: "0.68rem",
            fontWeight: 600,
            color: accentColor,
            fontFamily: "'DM Mono', monospace",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            marginBottom: "1rem",
            background: `${accentColor}14`,
            border: `1px solid ${accentColor}40`,
            borderRadius: "999px",
            padding: "0.2rem 0.6rem",
          }}
        >
          {label}
        </div>

        {/* Gauge centered */}
        <div style={{ display: "flex", justifyContent: "center", marginBottom: "1rem" }}>
          <ScoreGauge score={report.overall_score} size={160} />
        </div>

        {/* MPN + status */}
        <div style={{ textAlign: "center" }}>
          {report.input_mpn && (
            <div
              style={{
                fontFamily: "'DM Mono', monospace",
                fontSize: "1.1rem",
                fontWeight: 700,
                color: "var(--text-primary)",
                letterSpacing: "0.03em",
                marginBottom: "0.4rem",
              }}
            >
              {report.input_mpn}
            </div>
          )}
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "0.35rem",
              padding: "0.25rem 0.75rem",
              borderRadius: "999px",
              background: si.bg,
              border: `1px solid ${si.border}`,
              color: si.color,
              fontWeight: 600,
              fontSize: "0.8rem",
            }}
          >
            {si.emoji} {si.label}
          </span>
        </div>

        {report.created_at && (
          <p
            style={{
              marginTop: "0.875rem",
              fontSize: "0.65rem",
              color: "var(--text-dim)",
              fontFamily: "'DM Mono', monospace",
              textAlign: "center",
            }}
          >
            {new Date(report.created_at).toLocaleString("ko-KR")}
          </p>
        )}
      </div>

      {/* Heritage */}
      <div>
        <SectionLabel>헤리티지 매칭</SectionLabel>
        <HeritageMatches matches={report.heritage_matches} />
      </div>

      {/* Risk flags */}
      <div>
        <SectionLabel>위험 플래그</SectionLabel>
        <RiskFlags flags={report.risk_flags} />
      </div>
    </div>
  );
}

// ─── Input form ───────────────────────────────────────────────────────────────

type InputMode = "id" | "mpn";

interface FormState {
  mode: InputMode;
  idA: string;
  idB: string;
  mpnA: string;
  mpnB: string;
  licenseKey: string;
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function ComparePageClient() {
  const [form, setForm] = useState<FormState>({
    mode: "mpn",
    idA: "",
    idB: "",
    mpnA: "",
    mpnB: "",
    licenseKey: typeof window !== "undefined" ? getLicenseKey() : "",
  });

  const [reportA, setReportA] = useState<ScreeningReport | null>(null);
  const [reportB, setReportB] = useState<ScreeningReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCompare = useCallback(async () => {
    setError(null);
    setReportA(null);
    setReportB(null);
    setLoading(true);

    const key = form.licenseKey.trim();
    if (!key) {
      setError("라이선스 키를 입력해 주세요.");
      setLoading(false);
      return;
    }

    try {
      if (form.mode === "id") {
        const idA = form.idA.trim();
        const idB = form.idB.trim();
        if (!idA || !idB) {
          setError("리포트 ID A와 B를 모두 입력해 주세요.");
          setLoading(false);
          return;
        }
        const [a, b] = await Promise.all([
          getScreening(idA, key),
          getScreening(idB, key),
        ]);
        setReportA(a);
        setReportB(b);
      } else {
        const mpnA = form.mpnA.trim();
        const mpnB = form.mpnB.trim();
        if (!mpnA || !mpnB) {
          setError("MPN A와 B를 모두 입력해 주세요.");
          setLoading(false);
          return;
        }
        const [a, b] = await Promise.all([
          screenBjtByMpn(mpnA, key),
          screenBjtByMpn(mpnB, key),
        ]);
        setReportA(a);
        setReportB(b);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "비교 분석 중 오류가 발생했습니다.");
    } finally {
      setLoading(false);
    }
  }, [form]);

  const inputStyle: React.CSSProperties = {
    background: "var(--bg-panel)",
    border: "1px solid var(--border-dim)",
    borderRadius: "0.5rem",
    color: "var(--text-primary)",
    fontFamily: "'DM Sans', sans-serif",
    fontSize: "0.875rem",
    padding: "0.625rem 0.875rem",
    width: "100%",
    transition: "border-color 0.2s, box-shadow 0.2s",
    outline: "none",
  };

  const hasResults = reportA && reportB;
  const labelA = reportA?.input_mpn || "리포트 A";
  const labelB = reportB?.input_mpn || "리포트 B";

  return (
    <div className="animate-fade-up">
      {/* Breadcrumb */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          fontSize: "0.775rem",
          color: "var(--text-dim)",
          marginBottom: "1.75rem",
        }}
      >
        <a
          href="/screening"
          style={{ color: "var(--text-dim)", textDecoration: "none" }}
          onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--accent-cyan)")}
          onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = "var(--text-dim)")}
        >
          업스크리닝
        </a>
        <span>/</span>
        <span style={{ color: "var(--text-secondary)" }}>비교 분석</span>
      </div>

      {/* Page title */}
      <div style={{ marginBottom: "2rem" }}>
        <h1
          style={{
            fontSize: "1.375rem",
            fontWeight: 700,
            color: "var(--text-primary)",
            letterSpacing: "0.01em",
            marginBottom: "0.35rem",
          }}
        >
          비교 분석
        </h1>
        <p style={{ fontSize: "0.825rem", color: "var(--text-dim)" }}>
          두 BJT 부품의 우주 적합성 평가 결과를 나란히 비교합니다.
        </p>
      </div>

      {/* Input form */}
      <div
        className="card stat-card animate-fade-up-1"
        style={{ padding: "1.75rem", marginBottom: "2.5rem" }}
      >
        {/* Mode toggle */}
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            marginBottom: "1.5rem",
          }}
        >
          {(["mpn", "id"] as InputMode[]).map((m) => (
            <button
              key={m}
              onClick={() => setForm((f) => ({ ...f, mode: m }))}
              style={{
                background: form.mode === m ? "rgba(0,200,240,0.12)" : "transparent",
                border: `1px solid ${form.mode === m ? "rgba(0,200,240,0.35)" : "var(--border-dim)"}`,
                color: form.mode === m ? "var(--accent-cyan)" : "var(--text-secondary)",
                borderRadius: "0.4rem",
                padding: "0.3rem 0.875rem",
                fontSize: "0.8rem",
                fontWeight: 500,
                cursor: "pointer",
                transition: "all 0.15s",
              }}
            >
              {m === "mpn" ? "MPN으로 스크리닝" : "리포트 ID로 조회"}
            </button>
          ))}
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "1.25rem",
            marginBottom: "1.25rem",
          }}
          className="max-sm:grid-cols-1"
        >
          {/* A input */}
          <div>
            <label
              style={{
                display: "block",
                fontSize: "0.72rem",
                fontWeight: 600,
                color: "var(--accent-cyan)",
                letterSpacing: "0.06em",
                textTransform: "uppercase",
                marginBottom: "0.4rem",
                fontFamily: "'DM Mono', monospace",
              }}
            >
              {form.mode === "mpn" ? "MPN A" : "리포트 ID A"}
            </label>
            <input
              value={form.mode === "mpn" ? form.mpnA : form.idA}
              onChange={(e) =>
                setForm((f) =>
                  form.mode === "mpn"
                    ? { ...f, mpnA: e.target.value }
                    : { ...f, idA: e.target.value }
                )
              }
              placeholder={form.mode === "mpn" ? "예: 2N2222A" : "예: abc123..."}
              style={inputStyle}
              onFocus={(e) => {
                (e.target as HTMLInputElement).style.borderColor = "var(--accent-cyan-dim)";
                (e.target as HTMLInputElement).style.boxShadow = "0 0 0 3px rgba(0,200,240,0.08)";
              }}
              onBlur={(e) => {
                (e.target as HTMLInputElement).style.borderColor = "var(--border-dim)";
                (e.target as HTMLInputElement).style.boxShadow = "none";
              }}
              onKeyDown={(e) => e.key === "Enter" && handleCompare()}
            />
          </div>

          {/* B input */}
          <div>
            <label
              style={{
                display: "block",
                fontSize: "0.72rem",
                fontWeight: 600,
                color: "#8fa3c0",
                letterSpacing: "0.06em",
                textTransform: "uppercase",
                marginBottom: "0.4rem",
                fontFamily: "'DM Mono', monospace",
              }}
            >
              {form.mode === "mpn" ? "MPN B" : "리포트 ID B"}
            </label>
            <input
              value={form.mode === "mpn" ? form.mpnB : form.idB}
              onChange={(e) =>
                setForm((f) =>
                  form.mode === "mpn"
                    ? { ...f, mpnB: e.target.value }
                    : { ...f, idB: e.target.value }
                )
              }
              placeholder={form.mode === "mpn" ? "예: BC547B" : "예: def456..."}
              style={inputStyle}
              onFocus={(e) => {
                (e.target as HTMLInputElement).style.borderColor = "var(--accent-cyan-dim)";
                (e.target as HTMLInputElement).style.boxShadow = "0 0 0 3px rgba(0,200,240,0.08)";
              }}
              onBlur={(e) => {
                (e.target as HTMLInputElement).style.borderColor = "var(--border-dim)";
                (e.target as HTMLInputElement).style.boxShadow = "none";
              }}
              onKeyDown={(e) => e.key === "Enter" && handleCompare()}
            />
          </div>
        </div>

        {/* License key */}
        <div style={{ marginBottom: "1.25rem" }}>
          <label
            style={{
              display: "block",
              fontSize: "0.72rem",
              fontWeight: 600,
              color: "var(--text-dim)",
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              marginBottom: "0.4rem",
              fontFamily: "'DM Mono', monospace",
            }}
          >
            라이선스 키
          </label>
          <input
            value={form.licenseKey}
            onChange={(e) => setForm((f) => ({ ...f, licenseKey: e.target.value }))}
            placeholder="sk-..."
            type="password"
            style={{ ...inputStyle, fontFamily: "'DM Mono', monospace" }}
            onFocus={(e) => {
              (e.target as HTMLInputElement).style.borderColor = "var(--accent-cyan-dim)";
              (e.target as HTMLInputElement).style.boxShadow = "0 0 0 3px rgba(0,200,240,0.08)";
            }}
            onBlur={(e) => {
              (e.target as HTMLInputElement).style.borderColor = "var(--border-dim)";
              (e.target as HTMLInputElement).style.boxShadow = "none";
            }}
          />
        </div>

        {/* Error */}
        {error && (
          <div
            style={{
              background: "rgba(239,68,68,0.07)",
              border: "1px solid rgba(239,68,68,0.25)",
              borderRadius: "0.5rem",
              padding: "0.625rem 0.875rem",
              fontSize: "0.8rem",
              color: "var(--accent-red)",
              marginBottom: "1rem",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}
          >
            <span>❌</span>
            {error}
          </div>
        )}

        {/* Compare button */}
        <button
          onClick={handleCompare}
          disabled={loading}
          className="btn-primary"
          style={{
            width: "100%",
            opacity: loading ? 0.6 : 1,
            cursor: loading ? "not-allowed" : "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "0.5rem",
          }}
        >
          {loading ? (
            <>
              <div
                style={{
                  width: "14px",
                  height: "14px",
                  borderRadius: "50%",
                  border: "2px solid rgba(4,6,15,0.3)",
                  borderTopColor: "var(--bg-void)",
                  animation: "spin 0.9s linear infinite",
                  flexShrink: 0,
                }}
              />
              분석 중...
            </>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                <path d="M1 7h5M8 7h5M4 4l-3 3 3 3M10 4l3 3-3 3" />
              </svg>
              비교 분석
            </>
          )}
        </button>
      </div>

      {/* Loading placeholder */}
      {loading && (
        <div
          className="card animate-fade-up"
          style={{ padding: "3rem", textAlign: "center" }}
        >
          <Spinner label="두 부품을 동시에 분석하는 중..." />
        </div>
      )}

      {/* Results */}
      {hasResults && !loading && (
        <div className="animate-fade-up">
          {/* Side-by-side header panels */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "1.5rem",
              marginBottom: "2rem",
            }}
            className="max-sm:grid-cols-1"
          >
            <ReportPanel report={reportA} label="리포트 A" accentColor="var(--accent-cyan)" />
            <ReportPanel report={reportB} label="리포트 B" accentColor="#8fa3c0" />
          </div>

          {/* Score delta banner */}
          <div
            className="card animate-fade-up-1"
            style={{
              padding: "1.25rem 1.5rem",
              marginBottom: "1.5rem",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: "1rem",
              background: "rgba(255,255,255,0.02)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "1.5rem", flexWrap: "wrap" }}>
              <div>
                <span style={{ fontSize: "0.65rem", color: "var(--text-dim)", letterSpacing: "0.07em", textTransform: "uppercase", fontFamily: "'DM Mono', monospace", display: "block", marginBottom: "0.2rem" }}>
                  {labelA} 종합 점수
                </span>
                <span
                  style={{
                    fontSize: "1.5rem",
                    fontWeight: 700,
                    fontFamily: "'DM Mono', monospace",
                    color: reportA.overall_score >= 0.75 ? "#10b981" : reportA.overall_score >= 0.55 ? "#f59e0b" : "#ef4444",
                  }}
                >
                  {Math.round(reportA.overall_score * 100)}
                </span>
              </div>

              <div
                style={{
                  fontSize: "1.25rem",
                  color: "var(--text-dim)",
                  fontWeight: 300,
                }}
              >
                vs
              </div>

              <div>
                <span style={{ fontSize: "0.65rem", color: "var(--text-dim)", letterSpacing: "0.07em", textTransform: "uppercase", fontFamily: "'DM Mono', monospace", display: "block", marginBottom: "0.2rem" }}>
                  {labelB} 종합 점수
                </span>
                <span
                  style={{
                    fontSize: "1.5rem",
                    fontWeight: 700,
                    fontFamily: "'DM Mono', monospace",
                    color: reportB.overall_score >= 0.75 ? "#10b981" : reportB.overall_score >= 0.55 ? "#f59e0b" : "#ef4444",
                  }}
                >
                  {Math.round(reportB.overall_score * 100)}
                </span>
              </div>
            </div>

            {/* Delta */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
                padding: "0.5rem 1rem",
                borderRadius: "0.5rem",
                background: reportA.overall_score > reportB.overall_score
                  ? "rgba(16,185,129,0.08)"
                  : reportA.overall_score < reportB.overall_score
                  ? "rgba(239,68,68,0.08)"
                  : "rgba(255,255,255,0.04)",
                border: `1px solid ${
                  reportA.overall_score > reportB.overall_score
                    ? "rgba(16,185,129,0.25)"
                    : reportA.overall_score < reportB.overall_score
                    ? "rgba(239,68,68,0.25)"
                    : "var(--border-dim)"
                }`,
              }}
            >
              <span style={{ fontSize: "0.75rem", color: "var(--text-secondary)" }}>점수 차이</span>
              <span
                style={{
                  fontFamily: "'DM Mono', monospace",
                  fontWeight: 700,
                  fontSize: "1rem",
                  color:
                    reportA.overall_score > reportB.overall_score
                      ? "#10b981"
                      : reportA.overall_score < reportB.overall_score
                      ? "#ef4444"
                      : "var(--text-dim)",
                }}
              >
                {reportA.overall_score === reportB.overall_score
                  ? "동일"
                  : reportA.overall_score > reportB.overall_score
                  ? `A +${Math.round((reportA.overall_score - reportB.overall_score) * 100)}`
                  : `B +${Math.round((reportB.overall_score - reportA.overall_score) * 100)}`}
              </span>
            </div>
          </div>

          {/* Factor compare table */}
          <div className="animate-fade-up-2" style={{ marginBottom: "2rem" }}>
            <SectionLabel>평가 인자 상세 비교</SectionLabel>
            <FactorCompareTable
              factorsA={reportA.factor_scores}
              factorsB={reportB.factor_scores}
              labelA={labelA}
              labelB={labelB}
            />
          </div>

          {/* Back link */}
          <div style={{ paddingBottom: "2rem", display: "flex", gap: "0.75rem" }}>
            <a href="/screening" className="btn-ghost" style={{ fontSize: "0.825rem" }}>
              ← 새 분석
            </a>
            <a href={`/screening/${reportA.id}`} className="btn-ghost" style={{ fontSize: "0.825rem" }}>
              {labelA} 상세 보기
            </a>
            <a href={`/screening/${reportB.id}`} className="btn-ghost" style={{ fontSize: "0.825rem" }}>
              {labelB} 상세 보기
            </a>
          </div>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

"use client";
import { useState } from "react";
import Tooltip from "./Tooltip";
import FeedbackButton from "./FeedbackButton";

interface FactorSource {
  title: string;
  url: string;
  doi?: string;
  note?: string;
}

interface FactorScore {
  name: string;
  score: number;
  weight: number;
  coverage: number;
  rationale: string;
  sources: FactorSource[];
  value: number | null;
  direction: string;
}

interface FactorTableProps {
  factors: FactorScore[];
  reportId: string;
  licenseKey: string;
}

// Plain-Korean tooltip explanations per factor name (partial match)
const FACTOR_TOOLTIPS: Record<string, string> = {
  // BJT factors
  icbo: "ICBO는 누설 전류로, 우주 방사선 누적에 따라 가장 먼저 변하는 파라미터입니다. 값이 낮을수록 좋습니다.",
  vceo: "VCEO는 이미터 개방 시 컬렉터-베이스 간 최대 허용 전압입니다. 높을수록 우주 환경의 고전압 스트레스에 유리합니다.",
  vcbo: "VCBO는 이미터 개방 시 컬렉터-베이스 최대 역전압입니다. SEE(단일 이벤트 효과) 내성과 연관됩니다.",
  hfe: "hFE는 전류 이득으로, 방사선 누적 시 감소하는 경향이 있습니다. 초기값이 높을수록 방사선 열화 마진이 넓습니다.",
  ft: "ft는 전환 주파수로, 스위칭 속도를 나타냅니다. 우주 응용에서는 방사선 후 이득 대역폭 유지가 중요합니다.",
  pd: "최대 전력 소산(Pd)은 열 설계 마진과 직결됩니다. 우주선 내부 열관리는 지상보다 어렵기 때문에 마진이 충분해야 합니다.",
  tj: "최대 접합 온도(Tj_max)입니다. 우주 환경(-55°C ~ +150°C)에서 안정적으로 동작해야 합니다.",
  package: "패키지 형태는 방사선 차폐 효과와 우주 적합성에 영향을 줍니다. 금속 캔(TO-18, TO-39 등)이 우주 용도에 선호됩니다.",
  ic: "최대 컬렉터 전류(Ic_max)입니다. 우주 전원 시스템의 부하 전류 요건을 만족해야 합니다.",
  heritage: "헤리티지 점수는 유사 부품의 우주 비행 이력을 반영합니다. 검증된 헤리티지가 높을수록 신뢰성이 높습니다.",
  // MOSFET factors
  bvdss: "BVDSS(드레인-소스 항복전압)는 SEB/SEGR 내성의 핵심 지표입니다. 높을수록 단일 이벤트 번아웃 위험이 낮아집니다.",
  rds_on: "Rds(on)(온저항)은 TID 피폭 후 증가하는 경향이 있습니다. 초기값이 낮을수록 수명 말기 도통 손실 마진이 넓습니다.",
  vgs_th: "Vgs_th(게이트 문턱전압)은 TID에 의해 음의 방향으로 이동합니다. 초기값이 클수록 방사선 열화 마진이 넓습니다.",
  qg: "게이트 총 전하(Qg)가 낮을수록 SEGR 취약성이 감소하고 스위칭 손실 변화에 대한 여유가 커집니다.",
  idss: "IDSS(오프 상태 누설 전류)는 TID 피폭 후 증가합니다. 초기값이 낮을수록 방사선 후 누설 전력 예산 마진이 넓습니다.",
  gate_oxide: "게이트 산화막 두께는 TID 내성과 직결됩니다. 방사선 하드닝 공정의 핵심 구조 파라미터입니다.",
  power_derating: "전력 디레이팅 마진은 ECSS 기준 75% 이하 운용 여유를 나타냅니다. 진공 환경의 열 방산 한계를 고려합니다.",
  id_margin: "최대 드레인 전류(Id_max) 마진입니다. TID 후 RDS(on) 증가 시 과전류 열 파손 위험을 방지합니다.",
};

function getFactorTooltip(name: string): string {
  const lower = name.toLowerCase();
  for (const [key, tip] of Object.entries(FACTOR_TOOLTIPS)) {
    if (lower.includes(key)) return tip;
  }
  return `${name} 파라미터 — 우주 방사선 내성 평가에 사용되는 지표입니다.`;
}

function ScoreBar({ score }: { score: number }) {
  const color =
    score >= 0.75 ? "#10b981" : score >= 0.55 ? "#f59e0b" : "#ef4444";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        minWidth: "90px",
      }}
    >
      <div
        style={{
          flex: 1,
          height: "4px",
          background: "rgba(255,255,255,0.06)",
          borderRadius: "2px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${score * 100}%`,
            background: color,
            borderRadius: "2px",
          }}
        />
      </div>
      <span
        style={{
          fontSize: "0.72rem",
          fontFamily: "'DM Mono', monospace",
          color,
          fontWeight: 600,
          minWidth: "28px",
          textAlign: "right",
        }}
      >
        {(score * 100).toFixed(0)}
      </span>
    </div>
  );
}

function StatusChip({ score }: { score: number }) {
  if (score >= 0.75)
    return (
      <span
        className="chip"
        style={{
          background: "rgba(16,185,129,0.1)",
          borderColor: "rgba(16,185,129,0.3)",
          color: "#10b981",
          fontSize: "0.68rem",
        }}
      >
        양호
      </span>
    );
  if (score >= 0.55)
    return (
      <span
        className="chip"
        style={{
          background: "rgba(245,158,11,0.1)",
          borderColor: "rgba(245,158,11,0.3)",
          color: "#f59e0b",
          fontSize: "0.68rem",
        }}
      >
        주의
      </span>
    );
  return (
    <span
      className="chip"
      style={{
        background: "rgba(239,68,68,0.1)",
        borderColor: "rgba(239,68,68,0.3)",
        color: "#ef4444",
        fontSize: "0.68rem",
      }}
    >
      불량
    </span>
  );
}

export default function FactorTable({ factors, reportId, licenseKey }: FactorTableProps) {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<"weighted" | "score" | "weight">(
    "weighted"
  );
  const [sortAsc, setSortAsc] = useState(false);

  const sorted = [...factors].sort((a, b) => {
    let va: number, vb: number;
    if (sortKey === "weighted") {
      va = a.weight * a.score;
      vb = b.weight * b.score;
    } else if (sortKey === "score") {
      va = a.score;
      vb = b.score;
    } else {
      va = a.weight;
      vb = b.weight;
    }
    return sortAsc ? va - vb : vb - va;
  });

  const handleSort = (key: typeof sortKey) => {
    if (sortKey === key) setSortAsc((a) => !a);
    else {
      setSortKey(key);
      setSortAsc(false);
    }
  };

  const SortIcon = ({ active, asc }: { active: boolean; asc: boolean }) => (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      fill="none"
      style={{ display: "inline", marginLeft: "3px", opacity: active ? 1 : 0.3 }}
    >
      <path
        d={asc ? "M2 7l3-4 3 4" : "M2 3l3 4 3-4"}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );

  const tdStyle: React.CSSProperties = {
    padding: "0.75rem 1rem",
    fontSize: "0.825rem",
    color: "var(--text-secondary)",
    borderBottom: "1px solid var(--border-dim)",
    verticalAlign: "middle",
  };

  const thStyle: React.CSSProperties = {
    padding: "0.6rem 1rem",
    fontSize: "0.7rem",
    fontWeight: 600,
    color: "var(--text-dim)",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    borderBottom: "1px solid var(--border-dim)",
    textAlign: "left",
    cursor: "pointer",
    userSelect: "none",
    whiteSpace: "nowrap",
  };

  return (
    <div
      className="card"
      style={{ overflow: "hidden" }}
    >
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "rgba(255,255,255,0.02)" }}>
              <th style={thStyle}>인자</th>
              <th style={thStyle}>값</th>
              <th
                style={{ ...thStyle }}
                onClick={() => handleSort("score")}
              >
                점수
                <SortIcon active={sortKey === "score"} asc={sortAsc} />
              </th>
              <th
                style={thStyle}
                onClick={() => handleSort("weight")}
              >
                가중치
                <SortIcon active={sortKey === "weight"} asc={sortAsc} />
              </th>
              <th
                style={thStyle}
                onClick={() => handleSort("weighted")}
              >
                상태
                <SortIcon active={sortKey === "weighted"} asc={sortAsc} />
              </th>
              <th style={{ ...thStyle, cursor: "default" }}>상세</th>
              <th style={{ ...thStyle, cursor: "default" }}>평가</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((factor, i) => {
              const isExpanded = expandedIdx === i;
              return (
                <>
                  <tr
                    key={factor.name + i}
                    style={{
                      background: isExpanded
                        ? "rgba(0,200,240,0.03)"
                        : undefined,
                      transition: "background 0.15s",
                    }}
                  >
                    {/* Factor name + tooltip */}
                    <td style={{ ...tdStyle, color: "var(--text-primary)" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                        <span
                          style={{
                            fontFamily: "'DM Mono', monospace",
                            fontWeight: 500,
                            fontSize: "0.825rem",
                          }}
                        >
                          {factor.name}
                        </span>
                        <Tooltip text={getFactorTooltip(factor.name)}>
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              justifyContent: "center",
                              width: "16px",
                              height: "16px",
                              borderRadius: "50%",
                              border: "1px solid var(--border-bright)",
                              color: "var(--text-dim)",
                              fontSize: "0.6rem",
                              cursor: "default",
                              flexShrink: 0,
                            }}
                          >
                            ?
                          </span>
                        </Tooltip>
                      </div>
                    </td>

                    {/* Value */}
                    <td style={{ ...tdStyle, fontFamily: "'DM Mono', monospace" }}>
                      {factor.value !== null ? (
                        <span>{factor.value}</span>
                      ) : (
                        <span style={{ color: "var(--text-dim)", fontSize: "0.75rem" }}>
                          —
                        </span>
                      )}
                    </td>

                    {/* Score bar */}
                    <td style={tdStyle}>
                      <ScoreBar score={factor.score} />
                    </td>

                    {/* Weight */}
                    <td
                      style={{
                        ...tdStyle,
                        fontFamily: "'DM Mono', monospace",
                        color: "var(--text-secondary)",
                      }}
                    >
                      {factor.weight.toFixed(2)}
                    </td>

                    {/* Status chip */}
                    <td style={tdStyle}>
                      <StatusChip score={factor.score} />
                    </td>

                    {/* Expand toggle */}
                    <td style={tdStyle}>
                      <button
                        onClick={() =>
                          setExpandedIdx(isExpanded ? null : i)
                        }
                        style={{
                          background: "none",
                          border: "1px solid var(--border-dim)",
                          borderRadius: "0.375rem",
                          color: isExpanded
                            ? "var(--accent-cyan)"
                            : "var(--text-dim)",
                          padding: "0.2rem 0.5rem",
                          cursor: "pointer",
                          fontSize: "0.7rem",
                          transition: "all 0.15s",
                        }}
                        aria-expanded={isExpanded}
                      >
                        {isExpanded ? "접기" : "보기"}
                      </button>
                    </td>

                    {/* Feedback */}
                    <td style={tdStyle}>
                      <FeedbackButton
                        reportId={reportId}
                        factorName={factor.name}
                        licenseKey={licenseKey}
                      />
                    </td>
                  </tr>

                  {/* Expanded row */}
                  {isExpanded && (
                    <tr key={factor.name + i + "-exp"}>
                      <td
                        colSpan={7}
                        style={{
                          padding: "1rem 1.25rem 1.25rem",
                          background: "rgba(0,200,240,0.025)",
                          borderBottom: "1px solid var(--border-dim)",
                        }}
                      >
                        <p
                          style={{
                            fontSize: "0.825rem",
                            color: "var(--text-secondary)",
                            lineHeight: 1.65,
                            marginBottom:
                              factor.sources.length > 0 ? "0.875rem" : 0,
                          }}
                        >
                          {factor.rationale}
                        </p>
                        {factor.sources.length > 0 && (
                          <div>
                            <p
                              style={{
                                fontSize: "0.68rem",
                                color: "var(--text-dim)",
                                marginBottom: "0.4rem",
                                letterSpacing: "0.06em",
                                textTransform: "uppercase",
                              }}
                            >
                              참고 자료
                            </p>
                            <ul
                              style={{
                                display: "flex",
                                flexDirection: "column",
                                gap: "0.3rem",
                                listStyle: "none",
                                padding: 0,
                                margin: 0,
                              }}
                            >
                              {factor.sources.map((src, si) => (
                                <li key={si}>
                                  <a
                                    href={src.url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={{
                                      fontSize: "0.775rem",
                                      color: "var(--accent-cyan)",
                                      textDecoration: "none",
                                      display: "flex",
                                      alignItems: "center",
                                      gap: "0.35rem",
                                    }}
                                  >
                                    <svg
                                      width="10"
                                      height="10"
                                      viewBox="0 0 10 10"
                                      fill="none"
                                      stroke="currentColor"
                                      strokeWidth="1.5"
                                      style={{ flexShrink: 0 }}
                                    >
                                      <path d="M7 1h2v2M9 1L4 6M3 2H1v7h7V7" strokeLinecap="round" strokeLinejoin="round" />
                                    </svg>
                                    {src.title}
                                    {src.doi && (
                                      <span
                                        style={{
                                          color: "var(--text-dim)",
                                          fontSize: "0.68rem",
                                        }}
                                      >
                                        ({src.doi})
                                      </span>
                                    )}
                                  </a>
                                  {src.note && (
                                    <p
                                      style={{
                                        fontSize: "0.72rem",
                                        color: "var(--text-dim)",
                                        marginLeft: "1rem",
                                        marginTop: "0.15rem",
                                      }}
                                    >
                                      {src.note}
                                    </p>
                                  )}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

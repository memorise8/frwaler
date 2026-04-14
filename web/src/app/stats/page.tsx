"use client";
import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";

function MetricCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <div className="card stat-card p-6">
      <p
        style={{
          color: "var(--text-dim)",
          fontSize: "0.65rem",
          fontFamily: "'DM Mono', monospace",
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          marginBottom: "0.625rem",
        }}
      >
        {label}
      </p>
      <p
        style={{
          color: accent ? "var(--accent-cyan)" : "var(--text-primary)",
          fontSize: "2.25rem",
          fontWeight: 700,
          lineHeight: 1,
          marginBottom: "0.375rem",
          fontFamily: "'DM Mono', monospace",
        }}
        className={accent ? "text-glow" : ""}
      >
        {value}
      </p>
      {sub && (
        <p style={{ color: "var(--text-dim)", fontSize: "0.75rem" }}>{sub}</p>
      )}
    </div>
  );
}

export default function StatsPage() {
  const { data: productStats, error } = useSWR("/api/products/stats", fetcher, {
    refreshInterval: 15000,
  });

  const totalProducts = productStats?.total_products ?? 0;
  const sites: any[] = productStats?.sites ?? [];
  const totalHtml = sites.reduce((s: number, site: any) => s + (site.html_saved ?? 0), 0);

  return (
    <div>
      {/* Header */}
      <div className="animate-fade-up" style={{ marginBottom: "2rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.625rem", marginBottom: "0.375rem" }}>
          <h1
            style={{
              color: "var(--text-primary)",
              fontSize: "1.5rem",
              fontWeight: 700,
              letterSpacing: "-0.01em",
            }}
          >
            데이터베이스 통계
          </h1>
          <div className="pulse-dot" />
        </div>
        <p style={{ color: "var(--text-dim)", fontSize: "0.875rem" }}>
          실시간 수집 현황 · 15초마다 자동 갱신
        </p>
      </div>

      {error && (
        <div
          style={{
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.2)",
            color: "var(--accent-red)",
            borderRadius: "0.5rem",
            padding: "0.875rem 1rem",
            fontSize: "0.875rem",
            marginBottom: "1.5rem",
          }}
        >
          통계를 불러오지 못했습니다.
        </div>
      )}

      {/* Summary metrics */}
      <div
        className="grid grid-cols-2 md:grid-cols-3 gap-4 mb-10 animate-fade-up animate-fade-up-1"
      >
        <MetricCard
          label="총 제품 수"
          value={totalProducts.toLocaleString()}
          sub="전체 수집 부품"
          accent
        />
        <MetricCard
          label="수집 사이트"
          value={sites.length}
          sub="제조사 포털"
        />
        <MetricCard
          label="HTML 보관"
          value={totalHtml.toLocaleString()}
          sub="원본 저장 파일"
        />
      </div>

      {/* Per-site breakdown */}
      {sites.length > 0 && (
        <div className="animate-fade-up animate-fade-up-2">
          <h2
            style={{
              color: "var(--text-primary)",
              fontSize: "1rem",
              fontWeight: 600,
              marginBottom: "1rem",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}
          >
            <span
              style={{
                width: 4,
                height: 16,
                background: "var(--accent-cyan)",
                borderRadius: 2,
                display: "inline-block",
              }}
            />
            사이트별 현황
          </h2>

          <div
            className="card"
            style={{ overflow: "hidden" }}
          >
            {/* Table header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "2fr 1fr 1fr 1fr",
                padding: "0.625rem 1rem",
                background: "var(--bg-panel)",
                borderBottom: "1px solid var(--border-dim)",
              }}
            >
              {["사이트", "총 제품", "HTML 저장", "비율"].map((h) => (
                <span
                  key={h}
                  style={{
                    color: "var(--text-dim)",
                    fontSize: "0.65rem",
                    fontFamily: "'DM Mono', monospace",
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    textAlign: h === "사이트" ? "left" : "right",
                  }}
                >
                  {h}
                </span>
              ))}
            </div>

            {sites.map((site: any, i: number) => {
              const pct = totalProducts > 0
                ? ((site.total / totalProducts) * 100).toFixed(1)
                : "0.0";
              const barWidth = totalProducts > 0
                ? (site.total / totalProducts) * 100
                : 0;

              return (
                <div
                  key={site.site_id}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "2fr 1fr 1fr 1fr",
                    padding: "0.875rem 1rem",
                    borderBottom: i < sites.length - 1 ? "1px solid var(--border-dim)" : "none",
                    alignItems: "center",
                    background: i % 2 === 1 ? "rgba(255,255,255,0.01)" : "transparent",
                  }}
                >
                  <div>
                    <Link
                      href={`/products?site_id=${site.site_id}`}
                      style={{
                        color: "var(--accent-cyan)",
                        textDecoration: "none",
                        fontSize: "0.9rem",
                        fontWeight: 600,
                        textTransform: "capitalize",
                      }}
                    >
                      {site.site_id}
                    </Link>
                    {/* Mini bar */}
                    <div
                      style={{
                        marginTop: "0.375rem",
                        height: 2,
                        background: "var(--border-dim)",
                        borderRadius: 1,
                        overflow: "hidden",
                        maxWidth: 160,
                      }}
                    >
                      <div
                        style={{
                          width: `${barWidth}%`,
                          height: "100%",
                          background: "var(--accent-cyan)",
                          borderRadius: 1,
                          opacity: 0.6,
                        }}
                      />
                    </div>
                  </div>

                  <span
                    style={{
                      color: "var(--text-primary)",
                      fontSize: "0.875rem",
                      fontFamily: "'DM Mono', monospace",
                      textAlign: "right",
                    }}
                  >
                    {site.total.toLocaleString()}
                  </span>

                  <span
                    style={{
                      color: "var(--text-secondary)",
                      fontSize: "0.875rem",
                      fontFamily: "'DM Mono', monospace",
                      textAlign: "right",
                    }}
                  >
                    {(site.html_saved ?? 0).toLocaleString()}
                  </span>

                  <span
                    style={{
                      color: "var(--text-dim)",
                      fontSize: "0.8rem",
                      fontFamily: "'DM Mono', monospace",
                      textAlign: "right",
                    }}
                  >
                    {pct}%
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!productStats && !error && (
        <div
          style={{
            textAlign: "center",
            padding: "5rem 2rem",
            color: "var(--text-dim)",
            fontFamily: "'DM Mono', monospace",
            fontSize: "0.875rem",
          }}
        >
          통계 로딩 중...
        </div>
      )}
    </div>
  );
}

"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";

function StatCard({
  label,
  value,
  sub,
  delay,
}: {
  label: string;
  value: string | number;
  sub?: string;
  delay: number;
}) {
  return (
    <div
      className={`card stat-card p-6 animate-fade-up animate-fade-up-${delay}`}
    >
      <p
        style={{
          color: "var(--text-dim)",
          fontSize: "0.7rem",
          fontFamily: "'DM Mono', monospace",
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          marginBottom: "0.5rem",
        }}
      >
        {label}
      </p>
      <p
        style={{
          color: "var(--accent-cyan)",
          fontSize: "2rem",
          fontWeight: 700,
          lineHeight: 1,
          marginBottom: "0.25rem",
        }}
        className="text-glow"
      >
        {value}
      </p>
      {sub && (
        <p style={{ color: "var(--text-dim)", fontSize: "0.75rem" }}>{sub}</p>
      )}
    </div>
  );
}

export default function HomePage() {
  const router = useRouter();
  const [query, setQuery] = useState("");

  const { data: productStats } = useSWR("/api/products/stats", fetcher, {
    refreshInterval: 30000,
  });

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    if (query.trim()) {
      router.push(`/products?q=${encodeURIComponent(query.trim())}`);
    } else {
      router.push("/products");
    }
  }

  const totalProducts = productStats?.total_products ?? 0;
  const siteCount = productStats?.sites?.length ?? 0;
  const htmlSaved = productStats?.sites?.reduce(
    (sum: number, s: any) => sum + (s.html_saved ?? 0),
    0
  ) ?? 0;

  // Gather unique sites info
  const sites: any[] = productStats?.sites ?? [];

  return (
    <div>
      {/* ── Hero ──────────────────────────────────────────── */}
      <section
        className="hero-grid relative rounded-2xl overflow-hidden mb-12 animate-fade-up"
        style={{
          background:
            "linear-gradient(145deg, var(--bg-deep) 0%, var(--bg-panel) 100%)",
          border: "1px solid var(--border-dim)",
          padding: "4rem 3rem",
        }}
      >
        {/* Corner accent marks */}
        <span
          style={{
            position: "absolute",
            top: 16,
            left: 16,
            width: 20,
            height: 20,
            borderTop: "1.5px solid var(--accent-cyan)",
            borderLeft: "1.5px solid var(--accent-cyan)",
            opacity: 0.5,
          }}
        />
        <span
          style={{
            position: "absolute",
            bottom: 16,
            right: 16,
            width: 20,
            height: 20,
            borderBottom: "1.5px solid var(--accent-cyan)",
            borderRight: "1.5px solid var(--accent-cyan)",
            opacity: 0.5,
          }}
        />

        {/* Glow orb */}
        <div
          style={{
            position: "absolute",
            top: "-30%",
            right: "-5%",
            width: 400,
            height: 400,
            borderRadius: "50%",
            background:
              "radial-gradient(circle, rgba(0,200,240,0.06) 0%, transparent 70%)",
            pointerEvents: "none",
          }}
        />

        <div style={{ maxWidth: 680, position: "relative" }}>
          <div
            className="chip"
            style={{ marginBottom: "1.25rem", display: "inline-flex" }}
          >
            <span
              style={{
                fontFamily: "'DM Mono', monospace",
                letterSpacing: "0.08em",
              }}
            >
              SEMICONDUCTOR · PASSIVE · ACTIVE · RF
            </span>
          </div>

          <h1
            style={{
              fontSize: "clamp(1.75rem, 4vw, 2.75rem)",
              fontWeight: 700,
              lineHeight: 1.15,
              letterSpacing: "-0.02em",
              color: "var(--text-primary)",
              marginBottom: "0.75rem",
            }}
          >
            우주·항공급 전자부품
            <br />
            <span
              style={{ color: "var(--accent-cyan)" }}
              className="text-glow"
            >
              통합 데이터베이스
            </span>
          </h1>

          <p
            style={{
              color: "var(--text-secondary)",
              fontSize: "1rem",
              lineHeight: 1.6,
              marginBottom: "2rem",
            }}
          >
            Vishay 등 주요 반도체 제조사의 부품 데이터를 실시간 수집·정리합니다.
            데이터시트, 스펙, 재고 현황을 한곳에서 확인하세요.
          </p>

          <form onSubmit={handleSearch} className="flex gap-3" style={{ maxWidth: 560 }}>
            <div style={{ position: "relative", flex: 1 }}>
              <svg
                width="16"
                height="16"
                viewBox="0 0 16 16"
                fill="none"
                style={{
                  position: "absolute",
                  left: "0.875rem",
                  top: "50%",
                  transform: "translateY(-50%)",
                  color: "var(--text-dim)",
                  pointerEvents: "none",
                }}
              >
                <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.5" />
                <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="부품 번호, 브랜드, 카테고리 검색..."
                className="search-input w-full"
                style={{ padding: "0.75rem 1rem 0.75rem 2.5rem", fontSize: "0.9375rem" }}
              />
            </div>
            <button type="submit" className="btn-primary" style={{ whiteSpace: "nowrap" }}>
              검색
            </button>
          </form>

          <div className="flex gap-4 mt-4 flex-wrap">
            {["Resistor", "Capacitor", "Diode", "MOSFET", "Inductor"].map((term) => (
              <button
                key={term}
                onClick={() => router.push(`/products?q=${term}`)}
                style={{
                  color: "var(--text-dim)",
                  fontSize: "0.8rem",
                  fontFamily: "'DM Mono', monospace",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  padding: 0,
                  transition: "color 0.2s",
                }}
                onMouseOver={(e) =>
                  ((e.target as HTMLElement).style.color = "var(--accent-cyan)")
                }
                onMouseOut={(e) =>
                  ((e.target as HTMLElement).style.color = "var(--text-dim)")
                }
              >
                → {term}
              </button>
            ))}
          </div>
        </div>
      </section>

      {/* ── Stats ─────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-12">
        <StatCard
          label="총 제품 수"
          value={totalProducts.toLocaleString()}
          sub="수집된 부품"
          delay={1}
        />
        <StatCard
          label="데이터 사이트"
          value={siteCount}
          sub="제조사 포털"
          delay={2}
        />
        <StatCard
          label="HTML 저장"
          value={htmlSaved.toLocaleString()}
          sub="원본 보관"
          delay={3}
        />
        <StatCard
          label="상태"
          value="LIVE"
          sub="실시간 수집 중"
          delay={4}
        />
      </div>

      {/* ── Sites ─────────────────────────────────────────── */}
      {sites.length > 0 && (
        <section className="mb-12 animate-fade-up animate-fade-up-3">
          <div className="flex items-center justify-between mb-5">
            <h2
              style={{
                color: "var(--text-primary)",
                fontSize: "1rem",
                fontWeight: 600,
                letterSpacing: "0.02em",
              }}
            >
              수집 사이트
            </h2>
            <Link
              href="/stats"
              style={{
                color: "var(--accent-cyan)",
                fontSize: "0.8rem",
                fontFamily: "'DM Mono', monospace",
              }}
            >
              전체 통계 →
            </Link>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {sites.map((site: any, i: number) => (
              <Link
                key={site.site_id}
                href={`/products?site_id=${site.site_id}`}
                className="card p-5 flex items-center justify-between group"
                style={{ textDecoration: "none" }}
              >
                <div>
                  <p
                    style={{
                      color: "var(--text-primary)",
                      fontWeight: 600,
                      fontSize: "0.9rem",
                      marginBottom: "0.25rem",
                      textTransform: "capitalize",
                    }}
                  >
                    {site.site_id}
                  </p>
                  <p
                    style={{
                      color: "var(--text-dim)",
                      fontSize: "0.75rem",
                      fontFamily: "'DM Mono', monospace",
                    }}
                  >
                    {site.total.toLocaleString()} 제품
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    style={{
                      color: "var(--accent-cyan)",
                      fontSize: "0.75rem",
                      fontFamily: "'DM Mono', monospace",
                      opacity: 0,
                      transition: "opacity 0.2s",
                    }}
                    className="group-hover:opacity-100"
                  >
                    탐색 →
                  </span>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {/* ── Quick links ───────────────────────────────────── */}
      <section className="animate-fade-up animate-fade-up-4">
        <h2
          style={{
            color: "var(--text-primary)",
            fontSize: "1rem",
            fontWeight: 600,
            marginBottom: "1.25rem",
          }}
        >
          빠른 탐색
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "전체 제품 목록", href: "/products", icon: "▤" },
            { label: "카테고리별 탐색", href: "/categories", icon: "⊞" },
            { label: "데이터베이스 통계", href: "/stats", icon: "◈" },
            { label: "Vishay 부품", href: "/products?site_id=vishay", icon: "◇" },
          ].map(({ label, href, icon }) => (
            <Link
              key={href}
              href={href}
              className="card p-5 flex flex-col gap-3 group"
              style={{ textDecoration: "none" }}
            >
              <span
                style={{
                  color: "var(--accent-cyan)",
                  fontSize: "1.25rem",
                  opacity: 0.7,
                }}
              >
                {icon}
              </span>
              <span
                style={{
                  color: "var(--text-secondary)",
                  fontSize: "0.875rem",
                  fontWeight: 500,
                  transition: "color 0.2s",
                }}
                className="group-hover:text-white"
              >
                {label}
              </span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}

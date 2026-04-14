"use client";
import { useState, useEffect, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";

function ProductCard({ product }: { product: any }) {
  const isAvailable =
    product.availability &&
    !product.availability.toLowerCase().includes("out");

  return (
    <Link
      href={`/products/${product.id}`}
      className="card flex flex-col overflow-hidden group"
      style={{ textDecoration: "none" }}
    >
      {/* Image */}
      <div
        style={{
          height: 180,
          background: "var(--bg-panel)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
          borderBottom: "1px solid var(--border-dim)",
          flexShrink: 0,
        }}
      >
        {product.image_url ? (
          <img
            src={product.image_url}
            alt={product.name}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "contain",
              padding: "1rem",
              transition: "transform 0.3s ease",
            }}
            className="group-hover:scale-105"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <svg
            width="40"
            height="40"
            viewBox="0 0 40 40"
            fill="none"
            style={{ opacity: 0.15 }}
          >
            <rect x="10" y="10" width="20" height="20" rx="2" stroke="#00c8f0" strokeWidth="1.5" />
            <rect x="14" y="14" width="12" height="12" rx="1" fill="rgba(0,200,240,0.2)" stroke="#00c8f0" strokeWidth="1" />
            <line x1="4" y1="16" x2="10" y2="16" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="4" y1="20" x2="10" y2="20" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="4" y1="24" x2="10" y2="24" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="30" y1="16" x2="36" y2="16" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="30" y1="20" x2="36" y2="20" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="30" y1="24" x2="36" y2="24" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        )}
      </div>

      {/* Content */}
      <div style={{ padding: "0.875rem", flex: 1, display: "flex", flexDirection: "column", gap: "0.375rem" }}>
        {product.brand && (
          <p
            style={{
              color: "var(--accent-cyan)",
              fontSize: "0.65rem",
              fontFamily: "'DM Mono', monospace",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
            }}
          >
            {product.brand}
          </p>
        )}

        <p
          style={{
            color: "var(--text-primary)",
            fontSize: "0.875rem",
            fontWeight: 600,
            lineHeight: 1.3,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical" as any,
            overflow: "hidden",
          }}
        >
          {product.name || "(이름 없음)"}
        </p>

        {product.description && (
          <p
            style={{
              color: "var(--text-dim)",
              fontSize: "0.75rem",
              lineHeight: 1.4,
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical" as any,
              overflow: "hidden",
              marginTop: "0.125rem",
            }}
          >
            {product.description}
          </p>
        )}

        <div style={{ marginTop: "auto", paddingTop: "0.5rem", display: "flex", flexWrap: "wrap", gap: "0.375rem", alignItems: "center" }}>
          {product.category && (
            <span className="chip-neutral" style={{
              display: "inline-flex",
              alignItems: "center",
              background: "rgba(255,255,255,0.04)",
              border: "1px solid var(--border-dim)",
              color: "var(--text-secondary)",
              fontSize: "0.7rem",
              letterSpacing: "0.02em",
              padding: "0.15rem 0.5rem",
              borderRadius: "999px",
            }}>
              {product.category}
            </span>
          )}
          {product.availability && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                fontSize: "0.7rem",
                padding: "0.15rem 0.5rem",
                borderRadius: "999px",
                background: isAvailable ? "rgba(16,185,129,0.1)" : "rgba(239,68,68,0.1)",
                color: isAvailable ? "var(--accent-green)" : "var(--accent-red)",
                border: `1px solid ${isAvailable ? "rgba(16,185,129,0.2)" : "rgba(239,68,68,0.2)"}`,
              }}
            >
              {isAvailable ? "재고있음" : "품절"}
            </span>
          )}
        </div>

        <p
          style={{
            color: "var(--text-dim)",
            fontSize: "0.65rem",
            fontFamily: "'DM Mono', monospace",
            marginTop: "0.25rem",
          }}
        >
          {product.site_id}
        </p>
      </div>
    </Link>
  );
}

function ProductsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [query, setQuery] = useState(searchParams.get("q") || "");
  const [search, setSearch] = useState(searchParams.get("q") || "");
  const [siteId, setSiteId] = useState(searchParams.get("site_id") || "");
  const [page, setPage] = useState(1);

  // Sync URL params on mount
  useEffect(() => {
    const q = searchParams.get("q") || "";
    const s = searchParams.get("site_id") || "";
    setQuery(q);
    setSearch(q);
    setSiteId(s);
    setPage(1);
  }, [searchParams]);

  const params = new URLSearchParams({ page: String(page), limit: "20" });
  if (search) params.set("q", search);
  if (siteId) params.set("site_id", siteId);

  const { data, error } = useSWR(`/api/products?${params.toString()}`, fetcher);
  const { data: stats } = useSWR("/api/products/stats", fetcher);

  const totalPages = data ? Math.ceil(data.total / 20) : 0;

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setSearch(query);
    setPage(1);
    const p = new URLSearchParams();
    if (query.trim()) p.set("q", query.trim());
    if (siteId) p.set("site_id", siteId);
    router.push(`/products?${p.toString()}`);
  }

  function handleSiteChange(val: string) {
    setSiteId(val);
    setPage(1);
  }

  return (
    <div>
      {/* Header */}
      <div className="animate-fade-up" style={{ marginBottom: "1.75rem" }}>
        <h1
          style={{
            color: "var(--text-primary)",
            fontSize: "1.5rem",
            fontWeight: 700,
            letterSpacing: "-0.01em",
            marginBottom: "0.25rem",
          }}
        >
          제품 검색
        </h1>
        <p style={{ color: "var(--text-dim)", fontSize: "0.875rem" }}>
          {data ? (
            <>
              <span style={{ color: "var(--accent-cyan)", fontFamily: "'DM Mono', monospace" }}>
                {data.total.toLocaleString()}
              </span>
              {" "}개 부품 검색됨
            </>
          ) : (
            "데이터베이스 검색 중..."
          )}
        </p>
      </div>

      {/* Search bar */}
      <form
        onSubmit={handleSearch}
        className="animate-fade-up animate-fade-up-1"
        style={{ display: "flex", gap: "0.625rem", marginBottom: "1.5rem", flexWrap: "wrap" }}
      >
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <svg
            width="15"
            height="15"
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
            placeholder="부품 번호, 브랜드, 카테고리, 설명..."
            className="search-input w-full"
            style={{ padding: "0.625rem 1rem 0.625rem 2.25rem", fontSize: "0.9rem" }}
          />
        </div>

        {stats?.sites && (
          <select
            value={siteId}
            onChange={(e) => handleSiteChange(e.target.value)}
            className="search-input"
            style={{ padding: "0.625rem 2.25rem 0.625rem 0.875rem", fontSize: "0.875rem", minWidth: 160 }}
          >
            <option value="">전체 사이트</option>
            {stats.sites.map((s: any) => (
              <option key={s.site_id} value={s.site_id}>
                {s.site_id} ({s.total.toLocaleString()})
              </option>
            ))}
          </select>
        )}

        <button type="submit" className="btn-primary">
          검색
        </button>

        {(search || siteId) && (
          <button
            type="button"
            className="btn-ghost"
            onClick={() => {
              setQuery("");
              setSearch("");
              setSiteId("");
              setPage(1);
              router.push("/products");
            }}
          >
            초기화
          </button>
        )}
      </form>

      {/* Active filters */}
      {(search || siteId) && (
        <div
          className="animate-fade-up"
          style={{ display: "flex", gap: "0.5rem", marginBottom: "1.25rem", flexWrap: "wrap" }}
        >
          {search && (
            <span className="chip">
              검색: {search}
            </span>
          )}
          {siteId && (
            <span className="chip">
              사이트: {siteId}
            </span>
          )}
        </div>
      )}

      {/* Error */}
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
          데이터를 불러오지 못했습니다. 서버 연결을 확인해주세요.
        </div>
      )}

      {/* Loading skeleton */}
      {!data && !error && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
          {Array.from({ length: 10 }).map((_, i) => (
            <div
              key={i}
              className="card"
              style={{
                height: 280,
                animation: `pulseDot 1.5s ease-in-out ${i * 0.1}s infinite alternate`,
                opacity: 0.4,
              }}
            />
          ))}
        </div>
      )}

      {/* Grid */}
      {data && data.items.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4 mb-8">
          {data.items.map((product: any, i: number) => (
            <ProductCard key={product.id} product={product} />
          ))}
        </div>
      )}

      {/* Empty state */}
      {data && data.items.length === 0 && (
        <div
          style={{
            textAlign: "center",
            padding: "5rem 2rem",
            color: "var(--text-dim)",
          }}
        >
          <p style={{ fontSize: "2rem", marginBottom: "0.75rem", opacity: 0.3 }}>◈</p>
          <p style={{ fontSize: "1rem", fontWeight: 500, color: "var(--text-secondary)", marginBottom: "0.5rem" }}>
            검색 결과 없음
          </p>
          <p style={{ fontSize: "0.875rem" }}>
            다른 검색어나 필터를 시도해보세요.
          </p>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            gap: "0.5rem",
          }}
        >
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="btn-ghost"
            style={{ opacity: page === 1 ? 0.3 : 1 }}
          >
            ← 이전
          </button>

          {/* Page numbers */}
          <div style={{ display: "flex", gap: "0.25rem" }}>
            {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
              const start = Math.max(1, Math.min(page - 2, totalPages - 4));
              const p = start + i;
              return (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: "0.375rem",
                    fontSize: "0.875rem",
                    fontFamily: "'DM Mono', monospace",
                    background: p === page ? "var(--accent-cyan)" : "transparent",
                    color: p === page ? "var(--bg-void)" : "var(--text-secondary)",
                    border: p === page ? "none" : "1px solid var(--border-dim)",
                    cursor: "pointer",
                    transition: "all 0.15s",
                  }}
                >
                  {p}
                </button>
              );
            })}
          </div>

          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="btn-ghost"
            style={{ opacity: page === totalPages ? 0.3 : 1 }}
          >
            다음 →
          </button>
        </div>
      )}
    </div>
  );
}

export default function ProductsPage() {
  return (
    <Suspense fallback={
      <div style={{ color: "var(--text-dim)", padding: "4rem", textAlign: "center", fontFamily: "'DM Mono', monospace" }}>
        로딩 중...
      </div>
    }>
      <ProductsContent />
    </Suspense>
  );
}

"use client";
import { useMemo } from "react";
import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";

export default function CategoriesPage() {
  const { data, error } = useSWR(
    "/api/products/categories",
    fetcher,
    { revalidateOnFocus: false }
  );
  const { data: stats } = useSWR("/api/products/stats", fetcher);

  const categories = useMemo(() => {
    if (!data?.categories) return [];
    return data.categories.map((c: any) => ({
      name: c.category?.trim() || "미분류",
      count: c.count,
    }));
  }, [data]);

  const totalCategories = categories.length;

  return (
    <div>
      {/* Header */}
      <div className="animate-fade-up" style={{ marginBottom: "2rem" }}>
        <h1
          style={{
            color: "var(--text-primary)",
            fontSize: "1.5rem",
            fontWeight: 700,
            letterSpacing: "-0.01em",
            marginBottom: "0.25rem",
          }}
        >
          카테고리
        </h1>
        <p style={{ color: "var(--text-dim)", fontSize: "0.875rem" }}>
          {totalCategories > 0 ? (
            <>
              <span style={{ color: "var(--accent-cyan)", fontFamily: "'DM Mono', monospace" }}>
                {totalCategories}
              </span>
              {" "}개 카테고리
            </>
          ) : (
            "카테고리 로딩 중..."
          )}
        </p>
      </div>

      {/* Sites quick filter */}
      {stats?.sites && stats.sites.length > 0 && (
        <div
          className="animate-fade-up animate-fade-up-1"
          style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginBottom: "2rem" }}
        >
          <Link
            href="/products"
            className="btn-ghost"
            style={{ fontSize: "0.8rem", padding: "0.375rem 0.875rem", textDecoration: "none" }}
          >
            전체 제품
          </Link>
          {stats.sites.map((site: any) => (
            <Link
              key={site.site_id}
              href={`/products?site_id=${site.site_id}`}
              className="btn-ghost"
              style={{ fontSize: "0.8rem", padding: "0.375rem 0.875rem", textDecoration: "none" }}
            >
              {site.site_id}
              <span
                style={{
                  marginLeft: "0.375rem",
                  color: "var(--text-dim)",
                  fontFamily: "'DM Mono', monospace",
                  fontSize: "0.7rem",
                }}
              >
                {site.total.toLocaleString()}
              </span>
            </Link>
          ))}
        </div>
      )}

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
          카테고리를 불러오지 못했습니다.
        </div>
      )}

      {/* Loading state */}
      {!data && !error && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {Array.from({ length: 12 }).map((_, i) => (
            <div
              key={i}
              className="card"
              style={{ height: 80, opacity: 0.3 }}
            />
          ))}
        </div>
      )}

      {/* Category grid */}
      {categories.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
          {categories.map(({ name, count }: { name: string; count: number }, i: number) => {
            const isMiscategorized = name === "미분류";
            return (
              <Link
                key={name}
                href={`/products?q=${encodeURIComponent(name === "미분류" ? "" : name)}`}
                className={`card animate-fade-up animate-fade-up-${Math.min(5, (i % 5) + 1)}`}
                style={{
                  textDecoration: "none",
                  padding: "1rem",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.5rem",
                  position: "relative",
                  overflow: "hidden",
                }}
              >
                {/* Accent bar */}
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    bottom: 0,
                    width: 2,
                    background: isMiscategorized
                      ? "var(--border-dim)"
                      : "var(--accent-cyan)",
                    opacity: 0.6,
                  }}
                />

                <p
                  style={{
                    color: "var(--text-primary)",
                    fontSize: "0.875rem",
                    fontWeight: 500,
                    lineHeight: 1.3,
                    paddingLeft: "0.5rem",
                  }}
                >
                  {name}
                </p>
                <p
                  style={{
                    color: "var(--accent-cyan)",
                    fontSize: "0.75rem",
                    fontFamily: "'DM Mono', monospace",
                    paddingLeft: "0.5rem",
                    opacity: isMiscategorized ? 0.4 : 0.8,
                  }}
                >
                  {count.toLocaleString()}개
                </p>
              </Link>
            );
          })}
        </div>
      )}

      {categories.length === 0 && data && (
        <div
          style={{
            textAlign: "center",
            padding: "5rem 2rem",
            color: "var(--text-dim)",
          }}
        >
          <p style={{ fontSize: "2rem", marginBottom: "0.75rem", opacity: 0.3 }}>⊞</p>
          <p style={{ fontSize: "1rem", fontWeight: 500, color: "var(--text-secondary)" }}>
            카테고리 없음
          </p>
        </div>
      )}
    </div>
  );
}

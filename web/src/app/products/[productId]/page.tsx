"use client";
import { useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { fetcher, fetchProductHtml } from "@/lib/api";

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="spec-row"
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 2fr",
        padding: "0.625rem 0.875rem",
        gap: "1rem",
        borderBottom: "1px solid var(--border-dim)",
      }}
    >
      <span
        style={{
          color: "var(--text-dim)",
          fontSize: "0.8rem",
          fontFamily: "'DM Mono', monospace",
          letterSpacing: "0.04em",
        }}
      >
        {label}
      </span>
      <span style={{ color: "var(--text-primary)", fontSize: "0.875rem" }}>
        {value}
      </span>
    </div>
  );
}

export default function ProductDetail({
  params,
}: {
  params: { productId: string };
}) {
  const { data: product, error } = useSWR(
    `/api/products/${params.productId}`,
    fetcher
  );
  const [tab, setTab] = useState<"info" | "html">("info");
  const [htmlContent, setHtmlContent] = useState<string | null>(null);
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [selectedImage, setSelectedImage] = useState(0);

  async function loadHtml() {
    if (htmlContent) {
      setTab("html");
      return;
    }
    setHtmlLoading(true);
    try {
      const content = await fetchProductHtml(params.productId);
      setHtmlContent(content);
    } catch {
      setHtmlContent("<p style='color:#ef4444;padding:2rem'>HTML 불러오기 실패</p>");
    } finally {
      setHtmlLoading(false);
      setTab("html");
    }
  }

  const BackLink = () => (
    <Link
      href="/products"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.375rem",
        color: "var(--text-secondary)",
        fontSize: "0.8rem",
        fontFamily: "'DM Mono', monospace",
        textDecoration: "none",
        marginBottom: "1.5rem",
        transition: "color 0.2s",
      }}
      onMouseOver={(e) =>
        ((e.currentTarget as HTMLElement).style.color = "var(--accent-cyan)")
      }
      onMouseOut={(e) =>
        ((e.currentTarget as HTMLElement).style.color = "var(--text-secondary)")
      }
    >
      ← 목록으로
    </Link>
  );

  if (error) {
    return (
      <div>
        <BackLink />
        <div
          style={{
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.2)",
            color: "var(--accent-red)",
            borderRadius: "0.75rem",
            padding: "2rem",
            textAlign: "center",
            marginTop: "2rem",
          }}
        >
          상품을 불러올 수 없습니다.
        </div>
      </div>
    );
  }

  if (!product) {
    return (
      <div>
        <BackLink />
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "2rem",
            marginTop: "2rem",
          }}
        >
          {[1, 2].map((i) => (
            <div
              key={i}
              className="card"
              style={{ height: 360, opacity: 0.3 }}
            />
          ))}
        </div>
      </div>
    );
  }

  const images: string[] = product.image_urls?.length
    ? product.image_urls
    : product.image_url
    ? [product.image_url]
    : [];

  const isAvailable =
    product.availability &&
    !product.availability.toLowerCase().includes("out");

  const datasheetUrl = product.metadata?.datasheet_url ?? product.datasheet_url;

  return (
    <div className="animate-fade-up">
      <BackLink />

      {/* Breadcrumb */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          marginBottom: "1.5rem",
          fontSize: "0.8rem",
          color: "var(--text-dim)",
          fontFamily: "'DM Mono', monospace",
        }}
      >
        <Link href="/products" style={{ color: "var(--text-dim)", textDecoration: "none" }}>
          Products
        </Link>
        {product.category && (
          <>
            <span>/</span>
            <Link
              href={`/products?q=${encodeURIComponent(product.category)}`}
              style={{ color: "var(--text-dim)", textDecoration: "none" }}
            >
              {product.category}
            </Link>
          </>
        )}
        <span>/</span>
        <span style={{ color: "var(--text-secondary)" }}>
          {product.name?.substring(0, 40) ?? product.id}
        </span>
      </div>

      {/* Tab bar */}
      <div
        style={{
          display: "flex",
          gap: "0",
          marginBottom: "2rem",
          borderBottom: "1px solid var(--border-dim)",
        }}
      >
        {[
          { key: "info", label: "부품 정보" },
          { key: "html", label: htmlLoading ? "불러오는 중..." : "HTML 원본" },
        ].map(({ key, label }) => (
          <button
            key={key}
            onClick={key === "html" ? loadHtml : () => setTab("info")}
            disabled={htmlLoading && key === "html"}
            style={{
              padding: "0.625rem 1.25rem",
              fontSize: "0.875rem",
              fontWeight: 500,
              background: "transparent",
              border: "none",
              borderBottom: `2px solid ${tab === key ? "var(--accent-cyan)" : "transparent"}`,
              color:
                tab === key ? "var(--accent-cyan)" : "var(--text-secondary)",
              cursor: "pointer",
              transition: "color 0.2s, border-color 0.2s",
              marginBottom: "-1px",
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "info" && (
        <div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1.5fr",
              gap: "2.5rem",
              marginBottom: "2.5rem",
            }}
            className="product-grid"
          >
            {/* Images panel */}
            <div>
              <div
                className="card"
                style={{
                  height: 320,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  overflow: "hidden",
                  marginBottom: "0.75rem",
                  padding: "1.5rem",
                }}
              >
                {images.length > 0 ? (
                  <img
                    src={images[selectedImage]}
                    alt={product.name}
                    style={{
                      maxWidth: "100%",
                      maxHeight: "100%",
                      objectFit: "contain",
                    }}
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = "none";
                    }}
                  />
                ) : (
                  <div style={{ opacity: 0.15, textAlign: "center" }}>
                    <svg width="60" height="60" viewBox="0 0 40 40" fill="none">
                      <rect x="10" y="10" width="20" height="20" rx="2" stroke="#00c8f0" strokeWidth="1.5" />
                      <rect x="14" y="14" width="12" height="12" rx="1" fill="rgba(0,200,240,0.2)" stroke="#00c8f0" strokeWidth="1" />
                      <line x1="4" y1="16" x2="10" y2="16" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                      <line x1="4" y1="20" x2="10" y2="20" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                      <line x1="4" y1="24" x2="10" y2="24" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                      <line x1="30" y1="16" x2="36" y2="16" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                      <line x1="30" y1="20" x2="36" y2="20" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                      <line x1="30" y1="24" x2="36" y2="24" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
                    </svg>
                    <p style={{ color: "var(--text-dim)", fontSize: "0.8rem", marginTop: "0.75rem" }}>
                      이미지 없음
                    </p>
                  </div>
                )}
              </div>

              {images.length > 1 && (
                <div style={{ display: "flex", gap: "0.5rem", overflowX: "auto" }}>
                  {images.map((url, i) => (
                    <button
                      key={i}
                      onClick={() => setSelectedImage(i)}
                      style={{
                        width: 64,
                        height: 64,
                        flexShrink: 0,
                        background: "var(--bg-panel)",
                        border: `1px solid ${i === selectedImage ? "var(--accent-cyan)" : "var(--border-dim)"}`,
                        borderRadius: "0.375rem",
                        overflow: "hidden",
                        cursor: "pointer",
                        padding: "0.25rem",
                        transition: "border-color 0.2s",
                      }}
                    >
                      <img
                        src={url}
                        alt={`view ${i + 1}`}
                        style={{ width: "100%", height: "100%", objectFit: "contain" }}
                      />
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Details panel */}
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              {product.brand && (
                <p
                  style={{
                    color: "var(--accent-cyan)",
                    fontSize: "0.7rem",
                    fontFamily: "'DM Mono', monospace",
                    letterSpacing: "0.12em",
                    textTransform: "uppercase",
                  }}
                >
                  {product.brand}
                </p>
              )}

              <h1
                style={{
                  color: "var(--text-primary)",
                  fontSize: "clamp(1.25rem, 3vw, 1.75rem)",
                  fontWeight: 700,
                  lineHeight: 1.2,
                  letterSpacing: "-0.01em",
                }}
              >
                {product.name || "(이름 없음)"}
              </h1>

              {/* Status badges */}
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                {product.category && (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      background: "rgba(0,200,240,0.08)",
                      border: "1px solid rgba(0,200,240,0.2)",
                      color: "var(--accent-cyan)",
                      fontSize: "0.75rem",
                      padding: "0.2rem 0.6rem",
                      borderRadius: "999px",
                    }}
                  >
                    {product.category}
                  </span>
                )}
                {product.availability && (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      fontSize: "0.75rem",
                      padding: "0.2rem 0.6rem",
                      borderRadius: "999px",
                      background: isAvailable
                        ? "rgba(16,185,129,0.1)"
                        : "rgba(239,68,68,0.1)",
                      color: isAvailable
                        ? "var(--accent-green)"
                        : "var(--accent-red)",
                      border: `1px solid ${
                        isAvailable
                          ? "rgba(16,185,129,0.25)"
                          : "rgba(239,68,68,0.25)"
                      }`,
                    }}
                  >
                    {product.availability}
                  </span>
                )}
              </div>

              {product.description && (
                <div
                  style={{
                    background: "var(--bg-panel)",
                    border: "1px solid var(--border-dim)",
                    borderRadius: "0.5rem",
                    padding: "0.875rem",
                  }}
                >
                  <p
                    style={{
                      color: "var(--text-dim)",
                      fontSize: "0.7rem",
                      fontFamily: "'DM Mono', monospace",
                      letterSpacing: "0.08em",
                      marginBottom: "0.375rem",
                      textTransform: "uppercase",
                    }}
                  >
                    Description
                  </p>
                  <p
                    style={{
                      color: "var(--text-secondary)",
                      fontSize: "0.875rem",
                      lineHeight: 1.6,
                      whiteSpace: "pre-line",
                    }}
                  >
                    {product.description}
                  </p>
                </div>
              )}

              {/* Action buttons */}
              <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", paddingTop: "0.5rem" }}>
                {product.url && (
                  <a
                    href={product.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-primary"
                    style={{ display: "inline-flex", alignItems: "center", gap: "0.375rem", textDecoration: "none" }}
                  >
                    원본 사이트 보기
                    <span style={{ fontSize: "0.75rem" }}>↗</span>
                  </a>
                )}
                {datasheetUrl && (
                  <a
                    href={datasheetUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-ghost"
                    style={{ display: "inline-flex", alignItems: "center", gap: "0.375rem", textDecoration: "none" }}
                  >
                    데이터시트 다운로드
                    <span style={{ fontSize: "0.75rem" }}>↓</span>
                  </a>
                )}
              </div>

              {/* Meta */}
              <div
                style={{
                  borderTop: "1px solid var(--border-dim)",
                  paddingTop: "0.875rem",
                  display: "flex",
                  gap: "1.5rem",
                  flexWrap: "wrap",
                }}
              >
                <div>
                  <p style={{ color: "var(--text-dim)", fontSize: "0.65rem", fontFamily: "'DM Mono', monospace", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "0.2rem" }}>
                    Site
                  </p>
                  <p style={{ color: "var(--text-secondary)", fontSize: "0.8rem", fontFamily: "'DM Mono', monospace" }}>
                    {product.site_id}
                  </p>
                </div>
                {product.crawled_at && (
                  <div>
                    <p style={{ color: "var(--text-dim)", fontSize: "0.65rem", fontFamily: "'DM Mono', monospace", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "0.2rem" }}>
                      수집일
                    </p>
                    <p style={{ color: "var(--text-secondary)", fontSize: "0.8rem", fontFamily: "'DM Mono', monospace" }}>
                      {new Date(product.crawled_at).toLocaleDateString("ko-KR")}
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Specs table */}
          {product.specs && Object.keys(product.specs).length > 0 && (
            <div>
              <h2
                style={{
                  color: "var(--text-primary)",
                  fontSize: "1rem",
                  fontWeight: 600,
                  letterSpacing: "-0.01em",
                  marginBottom: "0.875rem",
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
                기술 스펙
              </h2>
              <div
                className="card"
                style={{ overflow: "hidden" }}
              >
                {Object.entries(product.specs).map(([key, val]) => (
                  <InfoRow key={key} label={key} value={String(val)} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "html" && (
        <div>
          {htmlContent ? (
            <iframe
              srcDoc={htmlContent}
              className="w-full"
              style={{
                height: "80vh",
                border: "1px solid var(--border-dim)",
                borderRadius: "0.75rem",
                background: "#fff",
              }}
              sandbox="allow-same-origin"
              title="HTML 원본"
            />
          ) : (
            <div
              style={{
                height: 200,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--text-dim)",
                fontFamily: "'DM Mono', monospace",
                fontSize: "0.875rem",
              }}
            >
              HTML을 불러오는 중...
            </div>
          )}
        </div>
      )}
    </div>
  );
}

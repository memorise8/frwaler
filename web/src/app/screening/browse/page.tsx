"use client";
import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { listProducts, getProductStats } from "@/lib/api";

type Product = {
  id: string;
  site_id: string;
  external_id: string | null;
  name: string;
  brand: string | null;
  category: string | null;
  device_type: string | null;
  description: string | null;
  url: string | null;
};

type Stats = {
  total: number;
  by_device_type: Record<string, number>;
  by_site: Record<string, number>;
  by_brand: Record<string, number>;
};

const DEVICE_OPTIONS: { key: string; label: string; supported: boolean }[] = [
  { key: "", label: "전체", supported: true },
  { key: "bjt", label: "BJT", supported: true },
  { key: "mosfet", label: "MOSFET", supported: true },
  { key: "diode", label: "Diode", supported: false },
  { key: "ic_linear", label: "Linear IC (LDO)", supported: false },
  { key: "ic_logic", label: "Logic IC", supported: false },
  { key: "ic_power", label: "Power IC", supported: false },
];

const DEVICE_BADGE_COLORS: Record<string, string> = {
  bjt: "#f59e0b",
  mosfet: "#10b981",
  diode: "#8b5cf6",
  ic_linear: "#06b6d4",
  ic_logic: "#6366f1",
  ic_power: "#ef4444",
};

function DeviceBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const color = DEVICE_BADGE_COLORS[type] || "#64748b";
  return (
    <span
      style={{
        fontSize: "0.62rem",
        fontFamily: "'DM Mono', monospace",
        padding: "2px 7px",
        borderRadius: "3px",
        background: `${color}22`,
        color,
        border: `1px solid ${color}55`,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {type.replace("ic_", "")}
    </span>
  );
}

export default function BrowseProductsPage() {
  const router = useRouter();
  const [licenseKey, setLicenseKey] = useState<string>("");
  const [deviceType, setDeviceType] = useState<string>("");
  const [siteId, setSiteId] = useState<string>("");
  const [query, setQuery] = useState<string>("");
  const [page, setPage] = useState<number>(1);
  const [products, setProducts] = useState<Product[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [stats, setStats] = useState<Stats | null>(null);

  const perPage = 24;

  useEffect(() => {
    setLicenseKey(localStorage.getItem("bjt_license_key") || "");
  }, []);

  useEffect(() => {
    if (!licenseKey) return;
    getProductStats(licenseKey)
      .then(setStats)
      .catch(() => setStats(null));
  }, [licenseKey]);

  const load = useCallback(async () => {
    if (!licenseKey) return;
    setLoading(true);
    setError("");
    try {
      const params: Record<string, string | number> = {
        page,
        per_page: perPage,
      };
      if (deviceType) params.device_type = deviceType;
      if (siteId) params.site_id = siteId;
      if (query.trim()) params.q = query.trim();
      const res = await listProducts(params as never, licenseKey);
      setProducts(res.products || []);
      setTotal(res.total || 0);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "로드 실패");
      setProducts([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [licenseKey, deviceType, siteId, query, page]);

  useEffect(() => {
    const t = setTimeout(load, query ? 300 : 0);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    setPage(1);
  }, [deviceType, siteId, query]);

  const handleSelect = (p: Product) => {
    if (p.device_type !== "bjt" && p.device_type !== "mosfet") {
      alert("현재 BJT와 MOSFET만 스크리닝 가능합니다.");
      return;
    }
    const mpn = p.external_id || p.name.split(/\s/)[0];
    const params = new URLSearchParams({ mpn, type: p.device_type });
    if (p.brand) params.set("brand", p.brand);
    router.push(`/screening?${params.toString()}`);
  };

  const totalPages = Math.max(1, Math.ceil(total / perPage));

  const siteOptions = stats
    ? [
        { key: "", label: "전체 사이트", count: stats.total },
        ...Object.entries(stats.by_site).map(([k, v]) => ({
          key: k,
          label: k.toUpperCase(),
          count: v,
        })),
      ]
    : [{ key: "", label: "전체 사이트", count: 0 }];

  return (
    <div className="animate-fade-up">
      {/* Header */}
      <div style={{ marginBottom: "1.5rem" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.625rem",
            marginBottom: "0.5rem",
          }}
        >
          <span
            style={{
              fontFamily: "'DM Mono', monospace",
              fontSize: "0.7rem",
              color: "var(--accent-cyan)",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              border: "1px solid rgba(0,200,240,0.25)",
              padding: "0.15rem 0.5rem",
              borderRadius: "4px",
            }}
          >
            부품 DB 탐색
          </span>
          <Link
            href="/screening"
            style={{
              fontSize: "0.75rem",
              color: "var(--text-dim)",
              textDecoration: "none",
            }}
          >
            ← 스크리닝 홈
          </Link>
        </div>
        <h1
          style={{
            fontSize: "1.5rem",
            fontWeight: 700,
            color: "var(--text-primary)",
            marginBottom: "0.35rem",
          }}
        >
          부품 데이터베이스 탐색
        </h1>
        <p style={{ fontSize: "0.825rem", color: "var(--text-secondary)" }}>
          {stats
            ? `${stats.total.toLocaleString()}개 부품 · ${Object.keys(stats.by_site).length}개 제조사 · BJT/MOSFET만 스크리닝 가능`
            : "56,064개 부품에서 필터 검색"}
        </p>
      </div>

      {!licenseKey && (
        <div
          style={{
            background: "rgba(245,158,11,0.08)",
            border: "1px solid rgba(245,158,11,0.3)",
            borderRadius: "0.5rem",
            padding: "0.75rem 1rem",
            fontSize: "0.8rem",
            color: "#fbbf24",
            marginBottom: "1rem",
          }}
        >
          라이선스 키가 필요합니다.{" "}
          <Link href="/screening" style={{ color: "inherit", textDecoration: "underline" }}>
            스크리닝 페이지
          </Link>
          에서 먼저 설정해주세요.
        </div>
      )}

      {/* Filters */}
      <div
        className="card"
        style={{
          padding: "1rem",
          marginBottom: "1rem",
          display: "flex",
          flexWrap: "wrap",
          gap: "0.75rem",
          alignItems: "flex-end",
        }}
      >
        <div style={{ flex: "1 1 240px" }}>
          <label
            style={{
              display: "block",
              fontSize: "0.68rem",
              color: "var(--text-dim)",
              letterSpacing: "0.05em",
              textTransform: "uppercase",
              marginBottom: "0.3rem",
              fontWeight: 600,
            }}
          >
            검색어
          </label>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="예: 2N2222, IRF, LM317..."
            style={{
              width: "100%",
              background: "var(--bg-panel)",
              border: "1px solid var(--border-dim)",
              borderRadius: "0.5rem",
              color: "var(--text-primary)",
              fontFamily: "'DM Sans', sans-serif",
              fontSize: "0.825rem",
              padding: "0.5rem 0.75rem",
              outline: "none",
            }}
            className="search-input"
          />
        </div>

        <div style={{ minWidth: "180px" }}>
          <label
            style={{
              display: "block",
              fontSize: "0.68rem",
              color: "var(--text-dim)",
              letterSpacing: "0.05em",
              textTransform: "uppercase",
              marginBottom: "0.3rem",
              fontWeight: 600,
            }}
          >
            부품 타입
          </label>
          <select
            value={deviceType}
            onChange={(e) => setDeviceType(e.target.value)}
            style={{
              width: "100%",
              background: "var(--bg-panel)",
              border: "1px solid var(--border-dim)",
              borderRadius: "0.5rem",
              color: "var(--text-primary)",
              fontSize: "0.825rem",
              padding: "0.5rem 0.75rem",
              outline: "none",
            }}
          >
            {DEVICE_OPTIONS.map((opt) => {
              const count = stats?.by_device_type[opt.key];
              const countStr = opt.key
                ? count !== undefined
                  ? ` (${count.toLocaleString()})`
                  : ""
                : "";
              return (
                <option key={opt.key} value={opt.key}>
                  {opt.label}
                  {countStr}
                  {opt.key && !opt.supported ? " — 스크리닝 미지원" : ""}
                </option>
              );
            })}
          </select>
        </div>

        <div style={{ minWidth: "180px" }}>
          <label
            style={{
              display: "block",
              fontSize: "0.68rem",
              color: "var(--text-dim)",
              letterSpacing: "0.05em",
              textTransform: "uppercase",
              marginBottom: "0.3rem",
              fontWeight: 600,
            }}
          >
            제조사 사이트
          </label>
          <select
            value={siteId}
            onChange={(e) => setSiteId(e.target.value)}
            style={{
              width: "100%",
              background: "var(--bg-panel)",
              border: "1px solid var(--border-dim)",
              borderRadius: "0.5rem",
              color: "var(--text-primary)",
              fontSize: "0.825rem",
              padding: "0.5rem 0.75rem",
              outline: "none",
            }}
          >
            {siteOptions.map((opt) => (
              <option key={opt.key} value={opt.key}>
                {opt.label}
                {opt.key ? ` (${opt.count.toLocaleString()})` : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Result meta */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "0.75rem",
          fontSize: "0.78rem",
          color: "var(--text-dim)",
        }}
      >
        <span>
          {loading
            ? "검색 중…"
            : total > 0
            ? `${total.toLocaleString()}개 결과 · ${page}/${totalPages} 페이지`
            : error
            ? error
            : "결과 없음"}
        </span>
      </div>

      {/* Product grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          gap: "0.75rem",
          minHeight: "200px",
        }}
      >
        {products.map((p) => {
          const mpn = p.external_id || p.name.split(/\s/)[0];
          const supported = p.device_type === "bjt" || p.device_type === "mosfet";
          return (
            <div
              key={p.id}
              className="card"
              onClick={() => handleSelect(p)}
              style={{
                padding: "0.75rem 0.875rem",
                cursor: supported ? "pointer" : "not-allowed",
                opacity: supported ? 1 : 0.55,
                transition: "all 0.15s",
                display: "flex",
                flexDirection: "column",
                gap: "0.4rem",
              }}
              onMouseEnter={(e) => {
                if (supported)
                  e.currentTarget.style.borderColor = "rgba(0,200,240,0.4)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "";
              }}
              title={supported ? "클릭하여 스크리닝 시작" : "현재 BJT/MOSFET만 지원"}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "0.5rem",
                }}
              >
                <span
                  style={{
                    fontSize: "0.88rem",
                    color: "var(--text-primary)",
                    fontFamily: "'DM Mono', monospace",
                    fontWeight: 600,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {mpn}
                </span>
                <DeviceBadge type={p.device_type} />
              </div>
              <div
                style={{
                  fontSize: "0.7rem",
                  color: "var(--text-dim)",
                  display: "flex",
                  gap: "0.4rem",
                }}
              >
                <span>{p.brand || "—"}</span>
                <span style={{ opacity: 0.5 }}>·</span>
                <span
                  style={{
                    textTransform: "uppercase",
                    fontFamily: "'DM Mono', monospace",
                  }}
                >
                  {p.site_id}
                </span>
              </div>
              {p.description && (
                <p
                  style={{
                    fontSize: "0.72rem",
                    color: "var(--text-secondary)",
                    margin: 0,
                    lineHeight: 1.4,
                    display: "-webkit-box",
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                  }}
                >
                  {p.description}
                </p>
              )}
            </div>
          );
        })}
      </div>

      {/* Pagination */}
      {total > perPage && (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            gap: "0.5rem",
            marginTop: "1.25rem",
            alignItems: "center",
          }}
        >
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1 || loading}
            style={{
              padding: "0.35rem 0.75rem",
              background: "var(--bg-panel)",
              border: "1px solid var(--border-dim)",
              borderRadius: "0.4rem",
              color: "var(--text-secondary)",
              cursor: page === 1 ? "not-allowed" : "pointer",
              opacity: page === 1 ? 0.4 : 1,
              fontSize: "0.78rem",
            }}
          >
            ← 이전
          </button>
          <span
            style={{
              fontSize: "0.75rem",
              color: "var(--text-dim)",
              fontFamily: "'DM Mono', monospace",
              padding: "0 0.5rem",
            }}
          >
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages || loading}
            style={{
              padding: "0.35rem 0.75rem",
              background: "var(--bg-panel)",
              border: "1px solid var(--border-dim)",
              borderRadius: "0.4rem",
              color: "var(--text-secondary)",
              cursor: page >= totalPages ? "not-allowed" : "pointer",
              opacity: page >= totalPages ? 0.4 : 1,
              fontSize: "0.78rem",
            }}
          >
            다음 →
          </button>
        </div>
      )}
    </div>
  );
}

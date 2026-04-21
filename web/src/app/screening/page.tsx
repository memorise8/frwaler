"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import DatasheetUpload from "@/components/screening/DatasheetUpload";
import MpnAutocomplete, { AutocompleteProduct } from "@/components/screening/MpnAutocomplete";
import { screenBjtByFile, screenBjtByMpn, screenMosfetByFile, screenMosfetByMpn } from "@/lib/api";

type QuickExample = { mpn: string; type: "bjt" | "mosfet"; label: string };

const QUICK_EXAMPLES: QuickExample[] = [
  { mpn: "2N2222A", type: "bjt", label: "2N2222A" },
  { mpn: "2N3904", type: "bjt", label: "2N3904" },
  { mpn: "IRF540N", type: "mosfet", label: "IRF540N" },
  { mpn: "JANSR2N2222AUB", type: "bjt", label: "JANSR2N2222AUB (우주급)" },
  { mpn: "JANSR2N7268", type: "mosfet", label: "JANSR2N7268 (우주급)" },
];

export default function ScreeningPage() {
  const router = useRouter();

  // Part type toggle
  const [partType, setPartType] = useState<"bjt" | "mosfet">("bjt");

  // License key (stored in localStorage, loaded after mount)
  const [licenseKey, setLicenseKey] = useState<string>("");

  useEffect(() => {
    setLicenseKey(localStorage.getItem("bjt_license_key") || "");
    // Read URL params (from browse page)
    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      const urlMpn = params.get("mpn");
      const urlType = params.get("type");
      const urlBrand = params.get("brand");
      if (urlType === "bjt" || urlType === "mosfet") setPartType(urlType);
      if (urlMpn) setMpn(urlMpn);
      if (urlBrand) setManufacturer(urlBrand);
    }
  }, []);

  // PDF panel state
  const [file, setFile] = useState<File | null>(null);
  const [mpnHint, setMpnHint] = useState("");
  const [mfgHint, setMfgHint] = useState("");
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfError, setPdfError] = useState("");

  // MPN panel state
  const [mpn, setMpn] = useState("");
  const [manufacturer, setManufacturer] = useState("");
  const [mpnLoading, setMpnLoading] = useState(false);
  const [mpnError, setMpnError] = useState("");

  const saveLicenseKey = (val: string) => {
    setLicenseKey(val);
    if (typeof window !== "undefined") {
      localStorage.setItem("bjt_license_key", val);
    }
  };

  const handlePdfSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setPdfError("");
    setPdfLoading(true);
    try {
      const screenFn = partType === "mosfet" ? screenMosfetByFile : screenBjtByFile;
      const report = await screenFn(file, {
        mpn: mpnHint || undefined,
        manufacturer: mfgHint || undefined,
        licenseKey,
      });
      router.push(`/screening/${report.id}`);
    } catch (err: unknown) {
      setPdfError(err instanceof Error ? err.message : "오류가 발생했습니다");
    } finally {
      setPdfLoading(false);
    }
  };

  const handleMpnSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mpn.trim()) return;
    setMpnError("");
    setMpnLoading(true);
    try {
      const screenFn = partType === "mosfet" ? screenMosfetByMpn : screenBjtByMpn;
      const report = await screenFn(
        mpn.trim(),
        licenseKey,
        manufacturer.trim() || undefined
      );
      router.push(`/screening/${report.id}`);
    } catch (err: unknown) {
      setMpnError(err instanceof Error ? err.message : "오류가 발생했습니다");
    } finally {
      setMpnLoading(false);
    }
  };

  const handleAutocompletePick = (p: AutocompleteProduct) => {
    if (p.device_type === "bjt" || p.device_type === "mosfet") {
      setPartType(p.device_type);
    }
    if (p.brand) setManufacturer(p.brand);
    setMpnError("");
  };

  const applyExample = (ex: QuickExample) => {
    setPartType(ex.type);
    setMpn(ex.mpn);
    setManufacturer("");
    setMpnError("");
  };

  const inputClass: React.CSSProperties = {
    width: "100%",
    background: "var(--bg-panel)",
    border: "1px solid var(--border-dim)",
    borderRadius: "0.5rem",
    color: "var(--text-primary)",
    fontFamily: "'DM Sans', sans-serif",
    fontSize: "0.875rem",
    padding: "0.625rem 0.875rem",
    outline: "none",
    transition: "border-color 0.2s",
  };

  const labelStyle: React.CSSProperties = {
    display: "block",
    fontSize: "0.75rem",
    color: "var(--text-dim)",
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    marginBottom: "0.375rem",
    fontWeight: 500,
  };

  const isMosfet = partType === "mosfet";

  return (
    <div className="animate-fade-up">
      {/* Page header */}
      <div style={{ marginBottom: "2rem" }}>
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
            {isMosfet ? "MOSFET 업스크리닝" : "BJT 업스크리닝"}
          </span>
          <span
            style={{
              width: "1px",
              height: "16px",
              background: "var(--border-dim)",
            }}
          />
          <span style={{ fontSize: "0.75rem", color: "var(--text-dim)" }}>
            우주 등급 적합성 평가
          </span>
        </div>
        <h1
          style={{
            fontSize: "1.75rem",
            fontWeight: 700,
            color: "var(--text-primary)",
            marginBottom: "0.5rem",
            letterSpacing: "-0.01em",
          }}
        >
          {isMosfet ? "MOSFET" : "BJT"} 업스크리닝 분석
        </h1>
        <p style={{ fontSize: "0.875rem", color: "var(--text-secondary)", maxWidth: "560px" }}>
          {isMosfet
            ? "MOSFET 데이터시트를 업로드하거나 부품 번호(MPN)를 입력하면, AI가 우주 방사선 환경 적합성을 자동 평가합니다."
            : "BJT 데이터시트를 업로드하거나 부품 번호(MPN)를 입력하면, AI가 우주 방사선 환경 적합성을 자동 평가합니다."}
          {" "}비전문가도 쉽게 이해할 수 있도록 한국어로 설명합니다.
        </p>
      </div>

      {/* BJT / MOSFET toggle + Quick start + Browse link */}
      <div
        style={{
          marginBottom: "1.5rem",
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.75rem 1rem",
        }}
      >
        <div
          style={{
            display: "inline-flex",
            background: "var(--bg-panel)",
            border: "1px solid var(--border-dim)",
            borderRadius: "0.625rem",
            padding: "3px",
            gap: "2px",
          }}
        >
          {(["bjt", "mosfet"] as const).map((pt) => (
            <button
              key={pt}
              onClick={() => setPartType(pt)}
              style={{
                padding: "0.35rem 1rem",
                borderRadius: "0.45rem",
                border: "none",
                cursor: "pointer",
                fontSize: "0.8rem",
                fontWeight: 600,
                fontFamily: "'DM Mono', monospace",
                letterSpacing: "0.04em",
                transition: "all 0.15s",
                background: partType === pt ? "var(--accent-cyan)" : "transparent",
                color: partType === pt ? "#000" : "var(--text-dim)",
              }}
            >
              {pt.toUpperCase()}
            </button>
          ))}
        </div>

        <span
          style={{
            fontSize: "0.7rem",
            color: "var(--text-dim)",
            letterSpacing: "0.05em",
            textTransform: "uppercase",
            fontFamily: "'DM Mono', monospace",
          }}
        >
          빠른 시작
        </span>

        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem" }}>
          {QUICK_EXAMPLES.map((ex) => (
            <button
              key={ex.mpn}
              type="button"
              onClick={() => applyExample(ex)}
              style={{
                padding: "0.25rem 0.65rem",
                borderRadius: "999px",
                border: "1px solid var(--border-dim)",
                background: "var(--bg-panel)",
                color: "var(--text-secondary)",
                fontSize: "0.72rem",
                fontFamily: "'DM Mono', monospace",
                cursor: "pointer",
                transition: "all 0.15s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "rgba(0,200,240,0.4)";
                e.currentTarget.style.color = "var(--accent-cyan)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "var(--border-dim)";
                e.currentTarget.style.color = "var(--text-secondary)";
              }}
            >
              {ex.label}
            </button>
          ))}
        </div>

        <Link
          href="/screening/browse"
          style={{
            marginLeft: "auto",
            fontSize: "0.75rem",
            color: "var(--text-dim)",
            textDecoration: "none",
            display: "inline-flex",
            alignItems: "center",
            gap: "0.3rem",
            borderBottom: "1px dashed var(--border-dim)",
            paddingBottom: "1px",
          }}
        >
          부품 번호를 모르시나요? 목록에서 찾기
          <span style={{ fontSize: "0.85rem" }}>→</span>
        </Link>
      </div>

      {/* License key input (collapsible) */}
      <details
        style={{ marginBottom: "1.5rem" }}
      >
        <summary
          style={{
            cursor: "pointer",
            fontSize: "0.8rem",
            color: "var(--text-dim)",
            userSelect: "none",
            listStyle: "none",
            display: "flex",
            alignItems: "center",
            gap: "0.4rem",
          }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M5 2a3 3 0 100 6 3 3 0 000-6zM8 8l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
          라이선스 키 설정
          {licenseKey && (
            <span style={{ color: "#10b981", fontSize: "0.7rem" }}>
              (저장됨)
            </span>
          )}
        </summary>
        <div style={{ marginTop: "0.75rem", maxWidth: "400px" }}>
          <input
            type="password"
            value={licenseKey}
            onChange={(e) => saveLicenseKey(e.target.value)}
            placeholder="X-License-Key 입력..."
            style={inputClass}
            className="search-input"
          />
          <p style={{ fontSize: "0.7rem", color: "var(--text-dim)", marginTop: "0.35rem" }}>
            브라우저에 저장됩니다. API 접근 권한이 있는 경우에만 필요합니다.
          </p>
        </div>
      </details>

      {/* Two-panel layout */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr auto 1fr",
          gap: "2rem",
          alignItems: "start",
        }}
      >
        {/* ── Left: PDF upload ── */}
        <div className="animate-fade-up animate-fade-up-1">
          <div style={{ marginBottom: "1rem" }}>
            <h2
              style={{
                fontSize: "0.95rem",
                fontWeight: 600,
                color: "var(--text-primary)",
                marginBottom: "0.25rem",
              }}
            >
              데이터시트 업로드
            </h2>
            <p style={{ fontSize: "0.775rem", color: "var(--text-secondary)" }}>
              {isMosfet ? "MOSFET" : "트랜지스터"} 규격서(PDF)를 업로드하면 파라미터를 자동 추출합니다
            </p>
          </div>

          <form onSubmit={handlePdfSubmit}>
            <div style={{ marginBottom: "1rem" }}>
              <DatasheetUpload onFile={(f) => setFile(f)} partLabel={isMosfet ? "MOSFET" : "트랜지스터"} />
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0.75rem",
                marginBottom: "1rem",
              }}
            >
              <div>
                <label style={labelStyle}>부품 번호 힌트 (선택)</label>
                <input
                  type="text"
                  value={mpnHint}
                  onChange={(e) => setMpnHint(e.target.value)}
                  placeholder="예: 2N2222A"
                  style={inputClass}
                  className="search-input"
                />
              </div>
              <div>
                <label style={labelStyle}>제조사 힌트 (선택)</label>
                <input
                  type="text"
                  value={mfgHint}
                  onChange={(e) => setMfgHint(e.target.value)}
                  placeholder="예: ON Semiconductor"
                  style={inputClass}
                  className="search-input"
                />
              </div>
            </div>

            {pdfError && (
              <div
                style={{
                  background: "rgba(239,68,68,0.08)",
                  border: "1px solid rgba(239,68,68,0.25)",
                  borderRadius: "0.5rem",
                  padding: "0.625rem 0.875rem",
                  fontSize: "0.8rem",
                  color: "#ef4444",
                  marginBottom: "0.875rem",
                }}
              >
                {pdfError}
              </div>
            )}

            <button
              type="submit"
              disabled={!file || pdfLoading}
              className="btn-primary"
              style={{
                width: "100%",
                opacity: !file || pdfLoading ? 0.5 : 1,
                cursor: !file || pdfLoading ? "not-allowed" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "0.5rem",
              }}
            >
              {pdfLoading ? (
                <>
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 14 14"
                    fill="none"
                    style={{ animation: "spin 1s linear infinite" }}
                  >
                    <circle
                      cx="7"
                      cy="7"
                      r="5.5"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeDasharray="22"
                      strokeDashoffset="8"
                      strokeLinecap="round"
                    />
                  </svg>
                  AI 분석 중...
                </>
              ) : (
                "데이터시트 분석 시작"
              )}
            </button>
          </form>
        </div>

        {/* ── Divider ── */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "0.5rem",
            paddingTop: "3rem",
          }}
        >
          <div
            style={{
              width: "1px",
              height: "60px",
              background: "var(--border-dim)",
            }}
          />
          <span
            style={{
              fontSize: "0.7rem",
              color: "var(--text-dim)",
              fontWeight: 600,
              letterSpacing: "0.08em",
            }}
          >
            또는
          </span>
          <div
            style={{
              width: "1px",
              height: "60px",
              background: "var(--border-dim)",
            }}
          />
        </div>

        {/* ── Right: MPN search ── */}
        <div className="animate-fade-up animate-fade-up-2">
          <div style={{ marginBottom: "1rem" }}>
            <h2
              style={{
                fontSize: "0.95rem",
                fontWeight: 600,
                color: "var(--text-primary)",
                marginBottom: "0.25rem",
              }}
            >
              부품 번호로 검색
            </h2>
            <p style={{ fontSize: "0.775rem", color: "var(--text-secondary)" }}>
              데이터베이스에서 MPN을 직접 조회하여 평가합니다
            </p>
          </div>

          <form onSubmit={handleMpnSubmit}>
            <div style={{ marginBottom: "0.75rem" }}>
              <label style={labelStyle}>부품 번호 (MPN) *</label>
              <MpnAutocomplete
                value={mpn}
                onChange={setMpn}
                onSelect={handleAutocompletePick}
                licenseKey={licenseKey}
                placeholder={isMosfet ? "예: JANSR2N7268, IRF540N" : "예: 2N2222A, BC547, 2N3904"}
                style={inputClass}
                className="search-input"
                deviceTypes={["bjt", "mosfet"]}
                required
              />
              <p style={{ fontSize: "0.68rem", color: "var(--text-dim)", marginTop: "0.35rem" }}>
                입력하면 56,064개 부품 DB에서 자동 추천 · 선택 시 타입 자동 전환
              </p>
            </div>

            <div style={{ marginBottom: "1rem" }}>
              <label style={labelStyle}>제조사 (선택)</label>
              <input
                type="text"
                value={manufacturer}
                onChange={(e) => setManufacturer(e.target.value)}
                placeholder="예: Texas Instruments"
                style={inputClass}
                className="search-input"
              />
            </div>

            {mpnError && (
              <div
                style={{
                  background: "rgba(239,68,68,0.08)",
                  border: "1px solid rgba(239,68,68,0.25)",
                  borderRadius: "0.5rem",
                  padding: "0.625rem 0.875rem",
                  fontSize: "0.8rem",
                  color: "#ef4444",
                  marginBottom: "0.875rem",
                }}
              >
                {mpnError}
              </div>
            )}

            <button
              type="submit"
              disabled={!mpn.trim() || mpnLoading}
              className="btn-primary"
              style={{
                width: "100%",
                opacity: !mpn.trim() || mpnLoading ? 0.5 : 1,
                cursor: !mpn.trim() || mpnLoading ? "not-allowed" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "0.5rem",
              }}
            >
              {mpnLoading ? (
                <>
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 14 14"
                    fill="none"
                    style={{ animation: "spin 1s linear infinite" }}
                  >
                    <circle
                      cx="7"
                      cy="7"
                      r="5.5"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeDasharray="22"
                      strokeDashoffset="8"
                      strokeLinecap="round"
                    />
                  </svg>
                  검색 중...
                </>
              ) : (
                "MPN 분석 시작"
              )}
            </button>
          </form>

          {/* How it works */}
          <div
            className="card"
            style={{
              marginTop: "1.5rem",
              padding: "1rem 1.125rem",
            }}
          >
            <p
              style={{
                fontSize: "0.68rem",
                color: "var(--text-dim)",
                letterSpacing: "0.06em",
                textTransform: "uppercase",
                marginBottom: "0.625rem",
                fontWeight: 600,
              }}
            >
              분석 방식
            </p>
            <ol
              style={{
                listStyle: "none",
                padding: 0,
                margin: 0,
                display: "flex",
                flexDirection: "column",
                gap: "0.45rem",
              }}
            >
              {[
                "데이터시트에서 전기적 파라미터 자동 추출",
                "10가지 우주 방사선 내성 인자 점수화",
                "헤리티지 데이터베이스와 유사도 비교",
                "종합 점수 및 위험 플래그 생성",
              ].map((step, i) => (
                <li
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "0.6rem",
                    fontSize: "0.775rem",
                    color: "var(--text-secondary)",
                  }}
                >
                  <span
                    style={{
                      width: "18px",
                      height: "18px",
                      borderRadius: "50%",
                      background: "rgba(0,200,240,0.1)",
                      border: "1px solid rgba(0,200,240,0.2)",
                      color: "var(--accent-cyan)",
                      fontSize: "0.65rem",
                      fontWeight: 700,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                      marginTop: "1px",
                    }}
                  >
                    {i + 1}
                  </span>
                  {step}
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>

      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}

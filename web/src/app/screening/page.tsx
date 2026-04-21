"use client";
import { useState, useRef } from "react";
import { useRouter } from "next/navigation";
import DatasheetUpload from "@/components/screening/DatasheetUpload";
import { screenBjtByFile, screenBjtByMpn, screenMosfetByFile, screenMosfetByMpn } from "@/lib/api";

export default function ScreeningPage() {
  const router = useRouter();

  // Device type tab
  const [deviceType, setDeviceType] = useState<'bjt' | 'mosfet'>('bjt');

  // License key (stored in localStorage)
  const [licenseKey, setLicenseKey] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("bjt_license_key") || "";
    }
    return "";
  });

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

  const handleTabChange = (tab: 'bjt' | 'mosfet') => {
    setDeviceType(tab);
    setFile(null);
    setMpnHint("");
    setMfgHint("");
    setPdfError("");
    setMpn("");
    setManufacturer("");
    setMpnError("");
  };

  const handlePdfSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setPdfError("");
    setPdfLoading(true);
    try {
      const screenFn = deviceType === 'bjt' ? screenBjtByFile : screenMosfetByFile;
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
      const screenFn = deviceType === 'bjt' ? screenBjtByMpn : screenMosfetByMpn;
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
            {deviceType === 'bjt' ? 'BJT 업스크리닝' : 'MOSFET 업스크리닝'}
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
          트랜지스터 업스크리닝 분석
        </h1>
        <p style={{ fontSize: "0.875rem", color: "var(--text-secondary)", maxWidth: "560px" }}>
          {deviceType === 'bjt'
            ? "BJT 데이터시트를 업로드하거나 부품 번호(MPN)를 입력하면, AI가 우주 방사선 환경 적합성을 자동 평가합니다. 비전문가도 쉽게 이해할 수 있도록 한국어로 설명합니다."
            : "MOSFET 데이터시트를 업로드하거나 부품 번호(MPN)를 입력하면, AI가 우주 방사선 환경 적합성을 자동 평가합니다."}
        </p>
      </div>

      {/* Device type tab toggle */}
      <div style={{ display: 'flex', gap: '0', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-dim)' }}>
        {(['bjt', 'mosfet'] as const).map(t => (
          <button key={t} onClick={() => handleTabChange(t)}
            style={{
              padding: '0.5rem 1.25rem',
              fontSize: '0.85rem',
              fontWeight: deviceType === t ? 600 : 400,
              color: deviceType === t ? 'var(--accent-cyan)' : 'var(--text-dim)',
              borderBottom: deviceType === t ? '2px solid var(--accent-cyan)' : '2px solid transparent',
              background: 'none',
              border: 'none',
              borderBottomWidth: '2px',
              borderBottomStyle: 'solid',
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
          >{t.toUpperCase()}</button>
        ))}
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
              트랜지스터 규격서(PDF)를 업로드하면 파라미터를 자동 추출합니다
            </p>
          </div>

          <form onSubmit={handlePdfSubmit}>
            <div style={{ marginBottom: "1rem" }}>
              <DatasheetUpload onFile={(f) => setFile(f)} />
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
              <input
                type="text"
                value={mpn}
                onChange={(e) => setMpn(e.target.value)}
                placeholder={deviceType === 'bjt' ? "예: 2N2222A, BC547, 2N3904" : "예: IRHNJ57130SE, 2N7002, IRF540"}
                required
                style={inputClass}
                className="search-input"
              />
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
                deviceType === 'bjt'
                  ? "10가지 우주 방사선 내성 인자 점수화"
                  : "10가지 MOSFET 우주 방사선 내성 인자 점수화 (Vth 시프트, BVDSS, Rds_on 등)",
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

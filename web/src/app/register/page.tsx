"use client";
import { useState } from "react";
import Link from "next/link";
import { registerLicense } from "@/lib/api";

type Phase = "form" | "loading" | "success" | "error";

export default function RegisterPage() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phase, setPhase] = useState<Phase>("form");
  const [generatedKey, setGeneratedKey] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [copied, setCopied] = useState(false);

  const isValidEmail = (v: string) =>
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !isValidEmail(email)) return;
    setPhase("loading");
    setErrorMsg("");
    try {
      const result = await registerLicense(name.trim(), email.trim());
      setGeneratedKey(result.key);
      setPhase("success");
    } catch (err: unknown) {
      let msg = "오류가 발생했습니다. 잠시 후 다시 시도해주세요.";
      if (err instanceof Error) {
        const raw = err.message;
        if (raw.includes("429") || raw.toLowerCase().includes("rate")) {
          msg = "오늘 발급 한도(5회)에 도달했습니다. 내일 다시 시도해주세요.";
        } else if (raw.includes("400") || raw.toLowerCase().includes("invalid")) {
          msg = "입력 정보를 확인해주세요.";
        } else {
          msg = raw;
        }
      }
      setErrorMsg(msg);
      setPhase("error");
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(generatedKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback: select text
    }
  };

  const inputStyle: React.CSSProperties = {
    width: "100%",
    background: "var(--bg-panel)",
    border: "1px solid var(--border-dim)",
    borderRadius: "0.5rem",
    color: "var(--text-primary)",
    fontFamily: "'DM Sans', sans-serif",
    fontSize: "0.875rem",
    padding: "0.625rem 0.875rem",
    outline: "none",
    transition: "border-color 0.2s, box-shadow 0.2s",
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
    <div
      className="animate-fade-up"
      style={{
        minHeight: "calc(100vh - 56px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "3rem 1.5rem",
      }}
    >
      <div style={{ width: "100%", maxWidth: "440px" }}>
        {/* Header badge */}
        <div
          className="animate-fade-up animate-fade-up-1"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.625rem",
            marginBottom: "1.25rem",
            justifyContent: "center",
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
            License
          </span>
        </div>

        {/* Title */}
        <div
          className="animate-fade-up animate-fade-up-2"
          style={{ textAlign: "center", marginBottom: "2rem" }}
        >
          <h1
            style={{
              fontSize: "1.875rem",
              fontWeight: 700,
              color: "var(--text-primary)",
              letterSpacing: "-0.02em",
              marginBottom: "0.625rem",
            }}
          >
            API 키 발급
          </h1>
          <p
            style={{
              fontSize: "0.875rem",
              color: "var(--text-secondary)",
              lineHeight: 1.6,
            }}
          >
            BJT/MOSFET 업스크리닝 서비스 이용을 위한
            <br />
            라이선스 키를 발급받으세요
          </p>
        </div>

        {/* ── SUCCESS STATE ── */}
        {phase === "success" && (
          <div className="animate-fade-up">
            {/* Key card */}
            <div
              className="card stat-card glow-cyan"
              style={{
                padding: "1.75rem",
                marginBottom: "1rem",
              }}
            >
              {/* Check icon */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.625rem",
                  marginBottom: "1.25rem",
                }}
              >
                <div
                  style={{
                    width: "32px",
                    height: "32px",
                    borderRadius: "50%",
                    background: "rgba(16,185,129,0.12)",
                    border: "1px solid rgba(16,185,129,0.35)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                    <path
                      d="M2.5 7.5L5.5 10.5L11.5 3.5"
                      stroke="#10b981"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <span
                  style={{
                    fontSize: "0.875rem",
                    fontWeight: 600,
                    color: "#10b981",
                  }}
                >
                  신청 완료
                </span>
              </div>
              {/* Pending notice */}
              <div
                style={{
                  background: "rgba(245,158,11,0.06)",
                  border: "1px solid rgba(245,158,11,0.2)",
                  borderRadius: "0.5rem",
                  padding: "0.75rem 1rem",
                  marginBottom: "1rem",
                  fontSize: "0.8rem",
                  color: "#f59e0b",
                  lineHeight: 1.6,
                }}
              >
                신청이 완료되었습니다. 관리자 승인 후 사용 가능합니다.
              </div>

              {/* Key display */}
              <div
                style={{
                  background: "var(--bg-deep)",
                  border: "1px solid var(--border-dim)",
                  borderRadius: "0.5rem",
                  padding: "1rem 1.125rem",
                  marginBottom: "0.875rem",
                  wordBreak: "break-all",
                }}
              >
                <p
                  style={{
                    fontSize: "0.65rem",
                    color: "var(--text-dim)",
                    letterSpacing: "0.06em",
                    textTransform: "uppercase",
                    marginBottom: "0.5rem",
                    fontWeight: 600,
                  }}
                >
                  발급된 키 (승인 시 이 키로 접속하세요)
                </p>
                <span
                  style={{
                    fontFamily: "'DM Mono', monospace",
                    fontSize: "0.95rem",
                    color: "var(--accent-cyan)",
                    letterSpacing: "0.04em",
                    lineHeight: 1.5,
                  }}
                >
                  {generatedKey}
                </span>
              </div>

              {/* Copy button */}
              <button
                onClick={handleCopy}
                className={copied ? "" : "btn-ghost"}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: "0.5rem",
                  padding: "0.625rem 1rem",
                  borderRadius: "0.5rem",
                  fontSize: "0.875rem",
                  fontWeight: 600,
                  cursor: "pointer",
                  transition: "all 0.2s",
                  ...(copied
                    ? {
                        background: "rgba(16,185,129,0.12)",
                        border: "1px solid rgba(16,185,129,0.3)",
                        color: "#10b981",
                      }
                    : {}),
                }}
              >
                {copied ? (
                  <>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                      <path
                        d="M2 7.5L5 10.5L12 3"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    복사됨
                  </>
                ) : (
                  <>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                      <rect
                        x="4.5"
                        y="1.5"
                        width="8"
                        height="9"
                        rx="1"
                        stroke="currentColor"
                        strokeWidth="1.25"
                      />
                      <path
                        d="M2 4.5v7a1 1 0 001 1h6"
                        stroke="currentColor"
                        strokeWidth="1.25"
                        strokeLinecap="round"
                      />
                    </svg>
                    복사
                  </>
                )}
              </button>
            </div>

            {/* Warning */}
            <div
              style={{
                background: "rgba(245,158,11,0.06)",
                border: "1px solid rgba(245,158,11,0.2)",
                borderRadius: "0.5rem",
                padding: "0.75rem 1rem",
                marginBottom: "1.25rem",
                display: "flex",
                gap: "0.625rem",
                alignItems: "flex-start",
              }}
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 14 14"
                fill="none"
                style={{ flexShrink: 0, marginTop: "1px" }}
              >
                <path
                  d="M7 1L13 12H1L7 1z"
                  stroke="#f59e0b"
                  strokeWidth="1.25"
                  strokeLinejoin="round"
                />
                <line
                  x1="7"
                  y1="5"
                  x2="7"
                  y2="8.5"
                  stroke="#f59e0b"
                  strokeWidth="1.25"
                  strokeLinecap="round"
                />
                <circle cx="7" cy="10.25" r="0.6" fill="#f59e0b" />
              </svg>
              <p
                style={{
                  fontSize: "0.775rem",
                  color: "#f59e0b",
                  lineHeight: 1.5,
                }}
              >
                이 키는 다시 확인할 수 없습니다. 안전한 곳에 보관하세요. 승인 후 이 키로 API를 사용하실 수 있습니다.
              </p>
            </div>

            {/* CTA link */}
            <Link
              href="/screening"
              className="btn-primary"
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "0.5rem",
                textDecoration: "none",
              }}
            >
              스크리닝 시작하기
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path
                  d="M3 7h8M8 4l3 3-3 3"
                  stroke="currentColor"
                  strokeWidth="1.75"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </Link>
          </div>
        )}

        {/* ── FORM / ERROR STATE ── */}
        {(phase === "form" || phase === "loading" || phase === "error") && (
          <div
            className="card animate-fade-up animate-fade-up-3"
            style={{ padding: "2rem" }}
          >
            {phase === "error" && (
              <div
                style={{
                  background: "rgba(239,68,68,0.08)",
                  border: "1px solid rgba(239,68,68,0.25)",
                  borderRadius: "0.5rem",
                  padding: "0.75rem 0.875rem",
                  fontSize: "0.8rem",
                  color: "#ef4444",
                  marginBottom: "1.25rem",
                  lineHeight: 1.5,
                }}
              >
                {errorMsg}
              </div>
            )}

            <form onSubmit={handleSubmit}>
              <div style={{ marginBottom: "1rem" }}>
                <label style={labelStyle} htmlFor="reg-name">
                  이름 *
                </label>
                <input
                  id="reg-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="홍길동"
                  required
                  disabled={phase === "loading"}
                  style={inputStyle}
                  className="search-input"
                />
              </div>

              <div style={{ marginBottom: "1.5rem" }}>
                <label style={labelStyle} htmlFor="reg-email">
                  이메일 *
                </label>
                <input
                  id="reg-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  required
                  disabled={phase === "loading"}
                  style={inputStyle}
                  className="search-input"
                />
                {email && !isValidEmail(email) && (
                  <p
                    style={{
                      fontSize: "0.7rem",
                      color: "#ef4444",
                      marginTop: "0.3rem",
                    }}
                  >
                    올바른 이메일 형식을 입력해주세요
                  </p>
                )}
              </div>

              <button
                type="submit"
                disabled={
                  phase === "loading" ||
                  !name.trim() ||
                  !isValidEmail(email)
                }
                className="btn-primary"
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: "0.5rem",
                  opacity:
                    phase === "loading" ||
                    !name.trim() ||
                    !isValidEmail(email)
                      ? 0.5
                      : 1,
                  cursor:
                    phase === "loading" ||
                    !name.trim() ||
                    !isValidEmail(email)
                      ? "not-allowed"
                      : "pointer",
                }}
              >
                {phase === "loading" ? (
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
                    발급 중...
                  </>
                ) : (
                  "키 발급받기"
                )}
              </button>
            </form>

            <p
              style={{
                fontSize: "0.7rem",
                color: "var(--text-dim)",
                textAlign: "center",
                marginTop: "1.25rem",
                lineHeight: 1.5,
              }}
            >
              하루 최대 5회 발급 가능합니다.
              <br />
              이미 키가 있으신가요?{" "}
              <Link
                href="/screening"
                style={{
                  color: "var(--accent-cyan)",
                  textDecoration: "none",
                }}
              >
                바로 스크리닝 시작하기
              </Link>
            </p>
          </div>
        )}
      </div>

      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}

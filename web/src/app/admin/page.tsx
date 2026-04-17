"use client";
import { useState, useEffect, useCallback } from "react";
import {
  adminListPending,
  adminListAll,
  adminApprove,
  adminReject,
  adminDeactivate,
} from "@/lib/api";

type PendingLicense = {
  key: string;
  owner: string;
  email: string | null;
  ip: string | null;
  created_at: string | null;
  active: number;
};

type LicenseInfo = {
  key: string;
  owner: string;
  email: string | null;
  plan: string;
  active: number;
  created_at: string | null;
  ip: string | null;
  daily_usage: number;
  monthly_usage: number;
};

export default function AdminPage() {
  const [password, setPassword] = useState("");
  const [inputPw, setInputPw] = useState("");
  const [loggedIn, setLoggedIn] = useState(false);
  const [pending, setPending] = useState<PendingLicense[]>([]);
  const [all, setAll] = useState<LicenseInfo[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [actionMsg, setActionMsg] = useState("");

  // Restore password from sessionStorage on mount
  useEffect(() => {
    const saved = sessionStorage.getItem("admin_pw");
    if (saved) {
      setPassword(saved);
      setLoggedIn(true);
    }
  }, []);

  const fetchData = useCallback(async (pw: string) => {
    setLoading(true);
    setError("");
    try {
      const [pendingData, allData] = await Promise.all([
        adminListPending(pw),
        adminListAll(pw),
      ]);
      setPending(pendingData);
      setAll(allData);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("403")) {
        setError("비밀번호가 올바르지 않습니다.");
        setLoggedIn(false);
        sessionStorage.removeItem("admin_pw");
      } else {
        setError(`데이터 로딩 실패: ${msg}`);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (loggedIn && password) {
      fetchData(password);
    }
  }, [loggedIn, password, fetchData]);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    sessionStorage.setItem("admin_pw", inputPw);
    setPassword(inputPw);
    setLoggedIn(true);
  };

  const handleAction = async (
    action: () => Promise<unknown>,
    successMsg: string
  ) => {
    setActionMsg("");
    setError("");
    try {
      await action();
      setActionMsg(successMsg);
      await fetchData(password);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`작업 실패: ${msg}`);
    }
  };

  const statusBadge = (active: number) => {
    if (active === 1)
      return (
        <span
          style={{
            background: "rgba(16,185,129,0.12)",
            border: "1px solid rgba(16,185,129,0.35)",
            color: "#10b981",
            borderRadius: "4px",
            padding: "0.1rem 0.45rem",
            fontSize: "0.7rem",
            fontWeight: 600,
          }}
        >
          활성
        </span>
      );
    return (
      <span
        style={{
          background: "rgba(245,158,11,0.1)",
          border: "1px solid rgba(245,158,11,0.3)",
          color: "#f59e0b",
          borderRadius: "4px",
          padding: "0.1rem 0.45rem",
          fontSize: "0.7rem",
          fontWeight: 600,
        }}
      >
        대기
      </span>
    );
  };

  const btn = (
    label: string,
    onClick: () => void,
    variant: "approve" | "reject" | "deactivate"
  ) => {
    const colors = {
      approve: { bg: "rgba(16,185,129,0.1)", border: "rgba(16,185,129,0.3)", color: "#10b981" },
      reject: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.25)", color: "#ef4444" },
      deactivate: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.25)", color: "#ef4444" },
    };
    const c = colors[variant];
    return (
      <button
        onClick={onClick}
        style={{
          background: c.bg,
          border: `1px solid ${c.border}`,
          color: c.color,
          borderRadius: "4px",
          padding: "0.2rem 0.5rem",
          fontSize: "0.75rem",
          cursor: "pointer",
          fontWeight: 600,
          marginRight: "0.25rem",
        }}
      >
        {label}
      </button>
    );
  };

  const thStyle: React.CSSProperties = {
    textAlign: "left",
    fontSize: "0.7rem",
    color: "var(--text-dim)",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    padding: "0.5rem 0.75rem",
    borderBottom: "1px solid var(--border-dim)",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };

  const tdStyle: React.CSSProperties = {
    padding: "0.6rem 0.75rem",
    fontSize: "0.8rem",
    color: "var(--text-secondary)",
    borderBottom: "1px solid rgba(30,45,74,0.5)",
    verticalAlign: "middle",
  };

  const cardStyle: React.CSSProperties = {
    background: "var(--bg-card)",
    border: "1px solid var(--border-dim)",
    borderRadius: "0.75rem",
    overflow: "hidden",
    marginBottom: "2rem",
  };

  return (
    <div
      style={{
        minHeight: "calc(100vh - 56px)",
        padding: "2rem 1.5rem",
        maxWidth: "1200px",
        margin: "0 auto",
      }}
    >
      {/* Header */}
      <div style={{ marginBottom: "2rem" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "1rem",
          }}
        >
          <div>
            <h1
              style={{
                fontSize: "1.5rem",
                fontWeight: 700,
                color: "var(--text-primary)",
                letterSpacing: "-0.02em",
                marginBottom: "0.25rem",
              }}
            >
              관리자 대시보드
            </h1>
            <p style={{ fontSize: "0.8rem", color: "var(--text-dim)" }}>
              라이선스 신청 승인 및 관리
            </p>
          </div>

          {/* Login form */}
          <form
            onSubmit={handleLogin}
            style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}
          >
            <input
              type="password"
              value={inputPw}
              onChange={(e) => setInputPw(e.target.value)}
              placeholder="관리자 비밀번호"
              style={{
                background: "var(--bg-panel)",
                border: "1px solid var(--border-dim)",
                borderRadius: "0.4rem",
                color: "var(--text-primary)",
                fontSize: "0.8rem",
                padding: "0.4rem 0.75rem",
                outline: "none",
                width: "180px",
              }}
            />
            <button
              type="submit"
              className="btn-primary"
              style={{
                padding: "0.4rem 0.875rem",
                fontSize: "0.8rem",
                borderRadius: "0.4rem",
              }}
            >
              {loggedIn ? "새로고침" : "로그인"}
            </button>
          </form>
        </div>
      </div>

      {/* Error / action feedback */}
      {error && (
        <div
          style={{
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.25)",
            borderRadius: "0.5rem",
            padding: "0.75rem 1rem",
            color: "#ef4444",
            fontSize: "0.8rem",
            marginBottom: "1rem",
          }}
        >
          {error}
        </div>
      )}
      {actionMsg && (
        <div
          style={{
            background: "rgba(16,185,129,0.08)",
            border: "1px solid rgba(16,185,129,0.25)",
            borderRadius: "0.5rem",
            padding: "0.75rem 1rem",
            color: "#10b981",
            fontSize: "0.8rem",
            marginBottom: "1rem",
          }}
        >
          {actionMsg}
        </div>
      )}

      {!loggedIn && !loading && (
        <div
          style={{
            textAlign: "center",
            color: "var(--text-dim)",
            fontSize: "0.875rem",
            marginTop: "4rem",
          }}
        >
          관리자 비밀번호를 입력하여 로그인하세요.
        </div>
      )}

      {loading && (
        <div
          style={{
            textAlign: "center",
            color: "var(--text-dim)",
            fontSize: "0.875rem",
            marginTop: "4rem",
          }}
        >
          로딩 중...
        </div>
      )}

      {loggedIn && !loading && (
        <>
          {/* Pending section */}
          <div style={cardStyle}>
            <div
              style={{
                padding: "1rem 1.25rem",
                borderBottom: "1px solid var(--border-dim)",
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
              }}
            >
              <span style={{ fontSize: "1rem" }}>📋</span>
              <span
                style={{
                  fontWeight: 700,
                  fontSize: "0.9rem",
                  color: "var(--text-primary)",
                }}
              >
                대기 중인 신청
              </span>
              <span
                style={{
                  background: "rgba(245,158,11,0.1)",
                  border: "1px solid rgba(245,158,11,0.3)",
                  color: "#f59e0b",
                  borderRadius: "999px",
                  padding: "0 0.45rem",
                  fontSize: "0.7rem",
                  fontWeight: 700,
                }}
              >
                {pending.length}건
              </span>
            </div>
            {pending.length === 0 ? (
              <div
                style={{
                  padding: "2rem",
                  textAlign: "center",
                  color: "var(--text-dim)",
                  fontSize: "0.8rem",
                }}
              >
                대기 중인 신청이 없습니다.
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <th style={thStyle}>이름</th>
                      <th style={thStyle}>이메일</th>
                      <th style={thStyle}>신청일</th>
                      <th style={thStyle}>IP</th>
                      <th style={thStyle}>키</th>
                      <th style={thStyle}>작업</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((p) => (
                      <tr key={p.key}>
                        <td style={tdStyle}>{p.owner}</td>
                        <td style={tdStyle}>{p.email ?? "-"}</td>
                        <td style={{ ...tdStyle, fontFamily: "'DM Mono', monospace", fontSize: "0.72rem" }}>
                          {p.created_at ? p.created_at.slice(0, 10) : "-"}
                        </td>
                        <td style={{ ...tdStyle, fontFamily: "'DM Mono', monospace", fontSize: "0.72rem" }}>
                          {p.ip ?? "-"}
                        </td>
                        <td
                          style={{
                            ...tdStyle,
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.68rem",
                            color: "var(--accent-cyan)",
                            maxWidth: "200px",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {p.key}
                        </td>
                        <td style={tdStyle}>
                          {btn("✅ 승인", () =>
                            handleAction(
                              () => adminApprove(p.key, password),
                              `${p.owner} 승인 완료`
                            ), "approve"
                          )}
                          {btn("❌ 거절", () =>
                            handleAction(
                              () => adminReject(p.key, password),
                              `${p.owner} 거절 완료`
                            ), "reject"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* All licenses section */}
          <div style={cardStyle}>
            <div
              style={{
                padding: "1rem 1.25rem",
                borderBottom: "1px solid var(--border-dim)",
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
              }}
            >
              <span style={{ fontSize: "1rem" }}>📊</span>
              <span
                style={{
                  fontWeight: 700,
                  fontSize: "0.9rem",
                  color: "var(--text-primary)",
                }}
              >
                전체 라이선스
              </span>
              <span
                style={{
                  background: "rgba(0,200,240,0.1)",
                  border: "1px solid rgba(0,200,240,0.25)",
                  color: "var(--accent-cyan)",
                  borderRadius: "999px",
                  padding: "0 0.45rem",
                  fontSize: "0.7rem",
                  fontWeight: 700,
                }}
              >
                {all.length}건
              </span>
            </div>
            {all.length === 0 ? (
              <div
                style={{
                  padding: "2rem",
                  textAlign: "center",
                  color: "var(--text-dim)",
                  fontSize: "0.8rem",
                }}
              >
                라이선스가 없습니다.
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <th style={thStyle}>키</th>
                      <th style={thStyle}>소유자</th>
                      <th style={thStyle}>플랜</th>
                      <th style={thStyle}>상태</th>
                      <th style={thStyle}>일 사용</th>
                      <th style={thStyle}>월 사용</th>
                      <th style={thStyle}>등록일</th>
                      <th style={thStyle}>작업</th>
                    </tr>
                  </thead>
                  <tbody>
                    {all.map((lic) => (
                      <tr key={lic.key}>
                        <td
                          style={{
                            ...tdStyle,
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.68rem",
                            color: "var(--accent-cyan)",
                            maxWidth: "180px",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {lic.key}
                        </td>
                        <td style={tdStyle}>{lic.owner}</td>
                        <td
                          style={{
                            ...tdStyle,
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.72rem",
                          }}
                        >
                          {lic.plan}
                        </td>
                        <td style={tdStyle}>{statusBadge(lic.active)}</td>
                        <td
                          style={{
                            ...tdStyle,
                            textAlign: "center",
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.72rem",
                          }}
                        >
                          {lic.daily_usage}
                        </td>
                        <td
                          style={{
                            ...tdStyle,
                            textAlign: "center",
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.72rem",
                          }}
                        >
                          {lic.monthly_usage}
                        </td>
                        <td
                          style={{
                            ...tdStyle,
                            fontFamily: "'DM Mono', monospace",
                            fontSize: "0.72rem",
                          }}
                        >
                          {lic.created_at ? lic.created_at.slice(0, 10) : "-"}
                        </td>
                        <td style={tdStyle}>
                          {lic.active === 0 ? (
                            <>
                              {btn("✅ 승인", () =>
                                handleAction(
                                  () => adminApprove(lic.key, password),
                                  `${lic.owner} 승인 완료`
                                ), "approve"
                              )}
                              {btn("❌ 거절", () =>
                                handleAction(
                                  () => adminReject(lic.key, password),
                                  `${lic.owner} 거절 완료`
                                ), "reject"
                              )}
                            </>
                          ) : (
                            btn("🚫 비활성화", () =>
                              handleAction(
                                () => adminDeactivate(lic.key, password),
                                `${lic.owner} 비활성화 완료`
                              ), "deactivate"
                            )
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

"use client";
import { useState, useRef, useEffect, useCallback } from "react";
import MessageBubble, { type Message } from "@/components/chat/MessageBubble";
import { type Candidate } from "@/components/chat/CandidateCard";

const PRO_API_BASE = "";

const EXAMPLE_PROMPTS = [
  "VCEO 60V 이상, Ic 1A 이상 SMD BJT 추천해줘",
  "10kΩ ±1% 0603 -55~125°C 우주용 저항 후보",
  "10µF 25V X7R 0805 MIL Class3 캐패시터",
  "스위칭용 트랜지스터 중 우주급 후보",
];

async function callChatApi(
  question: string,
  history: { role: "user" | "assistant"; content: string }[],
  licenseKey: string
): Promise<{ answer: string; candidates: Candidate[]; tokens_used: number }> {
  const res = await fetch(`${PRO_API_BASE}/pro/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(licenseKey ? { Authorization: `Bearer ${licenseKey}` } : {}),
    },
    body: JSON.stringify({ question, history }),
  });
  if (!res.ok) {
    let msg = `API 오류 (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // ignore
    }
    throw new Error(msg);
  }
  return res.json();
}

function Spinner() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      style={{ animation: "spin 0.8s linear infinite", flexShrink: 0 }}
      aria-hidden="true"
    >
      <circle
        cx="8"
        cy="8"
        r="6"
        stroke="currentColor"
        strokeWidth="2"
        strokeDasharray="28"
        strokeDashoffset="10"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [licenseKey, setLicenseKey] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (typeof window !== "undefined") {
      setLicenseKey(localStorage.getItem("bjt_license_key") || "");
    }
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  }, [input]);

  const buildHistory = useCallback(
    () =>
      messages
        .filter((m) => !m.error)
        .map((m) => ({ role: m.role, content: m.content })),
    [messages]
  );

  const sendMessage = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || loading) return;

      const userMsg: Message = { role: "user", content: q };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      setLoading(true);

      const history = buildHistory();

      try {
        const data = await callChatApi(q, history, licenseKey);
        const assistantMsg: Message = {
          role: "assistant",
          content: data.answer,
          candidates: data.candidates ?? [],
        };
        setMessages((prev) => [...prev, assistantMsg]);
      } catch (err) {
        const errMsg: Message = {
          role: "assistant",
          content: err instanceof Error ? err.message : "알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
          error: true,
        };
        setMessages((prev) => [...prev, errMsg]);
      } finally {
        setLoading(false);
        // Re-focus input
        setTimeout(() => textareaRef.current?.focus(), 50);
      }
    },
    [loading, licenseKey, buildHistory]
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  const isEmpty = messages.length === 0;

  return (
    <div
      className="animate-fade-up"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100vh - 7rem)",
        maxHeight: "900px",
        minHeight: "500px",
      }}
    >
      {/* ── Header ── */}
      <div style={{ marginBottom: "1.25rem", flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.625rem", marginBottom: "0.375rem" }}>
          <span
            style={{
              fontFamily: "'DM Mono', monospace",
              fontSize: "0.65rem",
              color: "var(--accent-cyan)",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              border: "1px solid rgba(0,200,240,0.25)",
              padding: "0.15rem 0.5rem",
              borderRadius: "4px",
            }}
          >
            AI CHAT
          </span>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.35rem",
              fontSize: "0.7rem",
              color: "var(--text-dim)",
            }}
          >
            <span className="pulse-dot" />
            실시간 추천
          </div>
        </div>
        <h1
          style={{
            fontSize: "clamp(1.25rem, 3vw, 1.625rem)",
            fontWeight: 700,
            color: "var(--text-primary)",
            letterSpacing: "-0.01em",
            marginBottom: "0.25rem",
          }}
        >
          부품 추천 챗
        </h1>
        <p style={{ fontSize: "0.825rem", color: "var(--text-secondary)" }}>
          자연어로 물어보면 DB에서 우주급 / AEC-Q 부품을 추천합니다.
        </p>
      </div>

      {/* ── Message area ── */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "1.25rem",
          padding: "1.25rem",
          background: "var(--bg-deep)",
          border: "1px solid var(--border-dim)",
          borderRadius: "0.875rem 0.875rem 0 0",
          borderBottom: "none",
        }}
      >
        {/* Empty state */}
        {isEmpty && (
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: "1.5rem",
              padding: "2rem 0",
            }}
          >
            <div
              style={{
                width: "56px",
                height: "56px",
                borderRadius: "50%",
                background: "radial-gradient(circle, rgba(0,200,240,0.12) 0%, transparent 70%)",
                border: "1px solid rgba(0,200,240,0.2)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "1.75rem",
              }}
            >
              🛰️
            </div>
            <div style={{ textAlign: "center" }}>
              <p style={{ fontSize: "0.9rem", color: "var(--text-secondary)", marginBottom: "0.25rem" }}>
                부품 사양을 자연어로 입력하세요
              </p>
              <p style={{ fontSize: "0.75rem", color: "var(--text-dim)" }}>
                우주급(MIL, JANS) · AEC-Q 인증 부품을 DB에서 검색합니다
              </p>
            </div>

            {/* Example prompts */}
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: "0.5rem",
                justifyContent: "center",
                maxWidth: "560px",
              }}
            >
              {EXAMPLE_PROMPTS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => sendMessage(p)}
                  disabled={loading}
                  style={{
                    padding: "0.4rem 0.875rem",
                    borderRadius: "999px",
                    border: "1px solid var(--border-dim)",
                    background: "var(--bg-panel)",
                    color: "var(--text-secondary)",
                    fontSize: "0.775rem",
                    fontFamily: "'DM Sans', sans-serif",
                    cursor: "pointer",
                    textAlign: "left",
                    transition: "all 0.15s",
                    lineHeight: 1.4,
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = "rgba(0,200,240,0.35)";
                    e.currentTarget.style.color = "var(--text-primary)";
                    e.currentTarget.style.background = "var(--bg-card)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = "var(--border-dim)";
                    e.currentTarget.style.color = "var(--text-secondary)";
                    e.currentTarget.style.background = "var(--bg-panel)";
                  }}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Messages */}
        {messages.map((msg, i) => (
          <MessageBubble key={i} msg={msg} />
        ))}

        {/* Loading indicator */}
        {loading && (
          <div style={{ display: "flex", alignItems: "flex-start", gap: "0.625rem" }}>
            <div
              style={{
                flexShrink: 0,
                width: "2rem",
                height: "2rem",
                borderRadius: "50%",
                background: "rgba(16,185,129,0.1)",
                border: "1px solid rgba(16,185,129,0.25)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "1rem",
              }}
              aria-hidden="true"
            >
              🛰️
            </div>
            <div
              style={{
                padding: "0.75rem 1rem",
                borderRadius: "0.25rem 1rem 1rem 1rem",
                background: "var(--bg-card)",
                border: "1px solid var(--border-dim)",
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
                fontSize: "0.825rem",
                color: "var(--text-dim)",
              }}
            >
              <Spinner />
              부품 DB 검색 중...
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Examples bar (non-empty state) ── */}
      {!isEmpty && (
        <div
          style={{
            flexShrink: 0,
            padding: "0.5rem 1rem",
            background: "var(--bg-panel)",
            borderLeft: "1px solid var(--border-dim)",
            borderRight: "1px solid var(--border-dim)",
            display: "flex",
            gap: "0.4rem",
            overflowX: "auto",
            scrollbarWidth: "none",
          }}
        >
          {EXAMPLE_PROMPTS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => sendMessage(p)}
              disabled={loading}
              style={{
                flexShrink: 0,
                padding: "0.2rem 0.65rem",
                borderRadius: "999px",
                border: "1px solid var(--border-dim)",
                background: "transparent",
                color: "var(--text-dim)",
                fontSize: "0.7rem",
                fontFamily: "'DM Mono', monospace",
                cursor: "pointer",
                whiteSpace: "nowrap",
                transition: "all 0.15s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "rgba(0,200,240,0.3)";
                e.currentTarget.style.color = "var(--accent-cyan)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "var(--border-dim)";
                e.currentTarget.style.color = "var(--text-dim)";
              }}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      {/* ── Input bar ── */}
      <div
        style={{
          flexShrink: 0,
          display: "flex",
          gap: "0.625rem",
          padding: "0.875rem",
          background: "var(--bg-card)",
          border: "1px solid var(--border-dim)",
          borderRadius: "0 0 0.875rem 0.875rem",
          alignItems: "flex-end",
        }}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="부품 사양을 자연어로 입력하세요... (Enter 전송 · Shift+Enter 줄바꿈)"
          rows={1}
          disabled={loading}
          style={{
            flex: 1,
            resize: "none",
            overflowY: "hidden",
            background: "var(--bg-panel)",
            border: "1px solid var(--border-dim)",
            borderRadius: "0.5rem",
            color: "var(--text-primary)",
            fontFamily: "'DM Sans', sans-serif",
            fontSize: "0.875rem",
            padding: "0.625rem 0.875rem",
            lineHeight: 1.55,
            outline: "none",
            transition: "border-color 0.2s, box-shadow 0.2s",
            minHeight: "40px",
            maxHeight: "160px",
          }}
          onFocus={(e) => {
            e.currentTarget.style.borderColor = "var(--accent-cyan-dim)";
            e.currentTarget.style.boxShadow = "0 0 0 3px rgba(0,200,240,0.08)";
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = "var(--border-dim)";
            e.currentTarget.style.boxShadow = "none";
          }}
        />

        <button
          type="button"
          onClick={() => sendMessage(input)}
          disabled={loading || !input.trim()}
          className="btn-primary"
          style={{
            padding: "0.625rem 1.125rem",
            display: "flex",
            alignItems: "center",
            gap: "0.4rem",
            flexShrink: 0,
            opacity: loading || !input.trim() ? 0.45 : 1,
            cursor: loading || !input.trim() ? "not-allowed" : "pointer",
            height: "40px",
          }}
        >
          {loading ? <Spinner /> : (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M1 7h12M7.5 1.5L13 7l-5.5 5.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
          <span style={{ fontSize: "0.825rem", fontWeight: 600 }}>
            {loading ? "검색 중" : "전송"}
          </span>
        </button>
      </div>

      {/* ── License key hint ── */}
      <div style={{ marginTop: "0.625rem", flexShrink: 0 }}>
        <details style={{ fontSize: "0.72rem" }}>
          <summary
            style={{
              cursor: "pointer",
              color: "var(--text-dim)",
              userSelect: "none",
              listStyle: "none",
              display: "inline-flex",
              alignItems: "center",
              gap: "0.35rem",
            }}
          >
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
              <path d="M5 2a3 3 0 100 6 3 3 0 000-6zM8 8l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            라이선스 키 설정
            {licenseKey && (
              <span style={{ color: "var(--accent-green)" }}>(저장됨)</span>
            )}
          </summary>
          <div style={{ marginTop: "0.5rem", maxWidth: "360px" }}>
            <input
              type="password"
              value={licenseKey}
              onChange={(e) => {
                setLicenseKey(e.target.value);
                localStorage.setItem("bjt_license_key", e.target.value);
              }}
              placeholder="Bearer 토큰 입력..."
              className="search-input"
              style={{
                width: "100%",
                padding: "0.5rem 0.75rem",
                fontSize: "0.775rem",
              }}
            />
          </div>
        </details>
      </div>

      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
        textarea::placeholder {
          color: var(--text-dim);
        }
      `}</style>
    </div>
  );
}

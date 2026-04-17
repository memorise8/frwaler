"use client";
import { useState } from "react";
import { submitFeedback } from "@/lib/api";

interface OverallFeedbackProps {
  reportId: string;
  licenseKey: string;
}

export default function OverallFeedback({ reportId, licenseKey }: OverallFeedbackProps) {
  const [voted, setVoted] = useState<"up" | "down" | null>(null);
  const [loading, setLoading] = useState(false);
  const [showComment, setShowComment] = useState(false);
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const handleVote = async (rating: "up" | "down") => {
    if (voted || loading) return;
    setVoted(rating);
    if (rating === "down") {
      setShowComment(true);
    } else {
      setLoading(true);
      try {
        await submitFeedback(reportId, rating, licenseKey, undefined, undefined);
        setSubmitted(true);
      } catch {
        // silently fail
      } finally {
        setLoading(false);
      }
    }
  };

  const handleSubmitComment = async () => {
    if (loading || !voted) return;
    setLoading(true);
    try {
      await submitFeedback(reportId, voted, licenseKey, undefined, comment || undefined);
      setSubmitted(true);
      setShowComment(false);
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  };

  const btnBase: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.5rem",
    padding: "0.6rem 1.25rem",
    borderRadius: "0.5rem",
    border: "1px solid var(--border-dim)",
    background: "none",
    fontSize: "0.875rem",
    fontWeight: 500,
    cursor: voted || loading ? "default" : "pointer",
    transition: "all 0.2s",
    opacity: loading ? 0.6 : 1,
  };

  return (
    <div
      className="card"
      style={{
        padding: "1.75rem 2rem",
        textAlign: "center",
        background: "rgba(255,255,255,0.015)",
      }}
    >
      {submitted ? (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "0.5rem",
          }}
        >
          <span style={{ fontSize: "1.5rem" }}>🙏</span>
          <p
            style={{
              fontSize: "0.875rem",
              color: "var(--text-secondary)",
              fontWeight: 500,
            }}
          >
            피드백을 보내주셔서 감사합니다.
          </p>
        </div>
      ) : (
        <>
          <p
            style={{
              fontSize: "0.875rem",
              color: "var(--text-secondary)",
              marginBottom: "1.25rem",
              fontWeight: 500,
            }}
          >
            이 분석 결과가 도움이 되었나요?
          </p>
          <div
            style={{
              display: "flex",
              justifyContent: "center",
              gap: "0.75rem",
              flexWrap: "wrap",
            }}
          >
            <button
              onClick={() => handleVote("up")}
              disabled={!!voted || loading}
              style={{
                ...btnBase,
                borderColor: voted === "up" ? "rgba(16,185,129,0.5)" : "var(--border-dim)",
                background: voted === "up" ? "rgba(16,185,129,0.1)" : "none",
                color: voted === "up" ? "#10b981" : "var(--text-secondary)",
              }}
              onMouseEnter={(e) => {
                if (!voted && !loading) {
                  const el = e.currentTarget as HTMLButtonElement;
                  el.style.borderColor = "rgba(16,185,129,0.4)";
                  el.style.color = "#10b981";
                  el.style.background = "rgba(16,185,129,0.06)";
                }
              }}
              onMouseLeave={(e) => {
                if (voted !== "up") {
                  const el = e.currentTarget as HTMLButtonElement;
                  el.style.borderColor = "var(--border-dim)";
                  el.style.color = "var(--text-secondary)";
                  el.style.background = "none";
                }
              }}
            >
              👍 도움됨
            </button>
            <button
              onClick={() => handleVote("down")}
              disabled={!!voted || loading}
              style={{
                ...btnBase,
                borderColor: voted === "down" ? "rgba(239,68,68,0.5)" : "var(--border-dim)",
                background: voted === "down" ? "rgba(239,68,68,0.1)" : "none",
                color: voted === "down" ? "#ef4444" : "var(--text-secondary)",
              }}
              onMouseEnter={(e) => {
                if (!voted && !loading) {
                  const el = e.currentTarget as HTMLButtonElement;
                  el.style.borderColor = "rgba(239,68,68,0.4)";
                  el.style.color = "#ef4444";
                  el.style.background = "rgba(239,68,68,0.06)";
                }
              }}
              onMouseLeave={(e) => {
                if (voted !== "down") {
                  const el = e.currentTarget as HTMLButtonElement;
                  el.style.borderColor = "var(--border-dim)";
                  el.style.color = "var(--text-secondary)";
                  el.style.background = "none";
                }
              }}
            >
              👎 개선 필요
            </button>
          </div>

          {/* Expandable comment box */}
          {showComment && (
            <div
              style={{
                marginTop: "1.25rem",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: "0.75rem",
                maxWidth: "480px",
                margin: "1.25rem auto 0",
              }}
            >
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="어떤 부분을 개선하면 좋을까요? (선택 사항)"
                rows={3}
                style={{
                  width: "100%",
                  background: "var(--bg-panel)",
                  border: "1px solid var(--border-dim)",
                  borderRadius: "0.5rem",
                  color: "var(--text-primary)",
                  fontFamily: "'DM Sans', sans-serif",
                  fontSize: "0.825rem",
                  padding: "0.625rem 0.875rem",
                  resize: "vertical",
                  outline: "none",
                  lineHeight: 1.6,
                  transition: "border-color 0.2s",
                }}
                onFocus={(e) => {
                  (e.currentTarget as HTMLTextAreaElement).style.borderColor = "var(--accent-cyan-dim)";
                }}
                onBlur={(e) => {
                  (e.currentTarget as HTMLTextAreaElement).style.borderColor = "var(--border-dim)";
                }}
              />
              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button
                  onClick={handleSubmitComment}
                  disabled={loading}
                  className="btn-primary"
                  style={{ fontSize: "0.8rem", padding: "0.45rem 1rem", opacity: loading ? 0.6 : 1 }}
                >
                  {loading ? "전송 중..." : "의견 보내기"}
                </button>
                <button
                  onClick={async () => {
                    if (loading || !voted) return;
                    setLoading(true);
                    try {
                      await submitFeedback(reportId, voted, licenseKey, undefined, undefined);
                      setSubmitted(true);
                      setShowComment(false);
                    } catch {
                      // silently fail
                    } finally {
                      setLoading(false);
                    }
                  }}
                  disabled={loading}
                  className="btn-ghost"
                  style={{ fontSize: "0.8rem", padding: "0.45rem 0.875rem" }}
                >
                  건너뛰기
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

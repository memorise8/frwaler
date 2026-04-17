"use client";
import { useState } from "react";
import { submitFeedback } from "@/lib/api";

interface FeedbackButtonProps {
  reportId: string;
  factorName?: string;
  licenseKey: string;
}

export default function FeedbackButton({
  reportId,
  factorName,
  licenseKey,
}: FeedbackButtonProps) {
  const [voted, setVoted] = useState<"up" | "down" | null>(null);
  const [loading, setLoading] = useState(false);

  const handleVote = async (rating: "up" | "down") => {
    if (voted || loading) return;
    setLoading(true);
    try {
      await submitFeedback(reportId, rating, licenseKey, factorName);
      setVoted(rating);
    } catch {
      // silently fail — feedback is non-critical
    } finally {
      setLoading(false);
    }
  };

  const btnBase: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: "26px",
    height: "26px",
    borderRadius: "0.375rem",
    border: "1px solid var(--border-dim)",
    background: "none",
    cursor: voted || loading ? "default" : "pointer",
    fontSize: "0.75rem",
    lineHeight: 1,
    transition: "all 0.15s",
    opacity: loading ? 0.5 : 1,
    flexShrink: 0,
  };

  const upActive = voted === "up";
  const downActive = voted === "down";

  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.25rem",
      }}
    >
      <button
        onClick={() => handleVote("up")}
        disabled={!!voted || loading}
        title="도움됨"
        style={{
          ...btnBase,
          borderColor: upActive ? "rgba(16,185,129,0.5)" : "var(--border-dim)",
          background: upActive ? "rgba(16,185,129,0.12)" : "none",
          color: upActive ? "#10b981" : "var(--text-dim)",
        }}
        onMouseEnter={(e) => {
          if (!voted && !loading) {
            (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(16,185,129,0.4)";
            (e.currentTarget as HTMLButtonElement).style.color = "#10b981";
          }
        }}
        onMouseLeave={(e) => {
          if (!upActive) {
            (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--border-dim)";
            (e.currentTarget as HTMLButtonElement).style.color = "var(--text-dim)";
          }
        }}
      >
        👍
      </button>
      <button
        onClick={() => handleVote("down")}
        disabled={!!voted || loading}
        title="개선 필요"
        style={{
          ...btnBase,
          borderColor: downActive ? "rgba(239,68,68,0.5)" : "var(--border-dim)",
          background: downActive ? "rgba(239,68,68,0.12)" : "none",
          color: downActive ? "#ef4444" : "var(--text-dim)",
        }}
        onMouseEnter={(e) => {
          if (!voted && !loading) {
            (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(239,68,68,0.4)";
            (e.currentTarget as HTMLButtonElement).style.color = "#ef4444";
          }
        }}
        onMouseLeave={(e) => {
          if (!downActive) {
            (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--border-dim)";
            (e.currentTarget as HTMLButtonElement).style.color = "var(--text-dim)";
          }
        }}
      >
        👎
      </button>
    </div>
  );
}

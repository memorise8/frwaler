"use client";
import CandidateCard, { type Candidate } from "./CandidateCard";

export type Message = {
  role: "user" | "assistant";
  content: string;
  candidates?: Candidate[];
  error?: boolean;
};

export default function MessageBubble({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: isUser ? "row-reverse" : "row",
        alignItems: "flex-start",
        gap: "0.625rem",
        width: "100%",
      }}
    >
      {/* Avatar */}
      <div
        style={{
          flexShrink: 0,
          width: "2rem",
          height: "2rem",
          borderRadius: "50%",
          background: isUser ? "rgba(0,200,240,0.12)" : "rgba(16,185,129,0.1)",
          border: isUser ? "1px solid rgba(0,200,240,0.25)" : "1px solid rgba(16,185,129,0.25)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "1rem",
          lineHeight: 1,
          marginTop: "0.125rem",
        }}
        aria-hidden="true"
      >
        {isUser ? "🙋" : "🛰️"}
      </div>

      {/* Content column */}
      <div
        style={{
          flex: 1,
          maxWidth: isUser ? "75%" : "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          alignItems: isUser ? "flex-end" : "flex-start",
        }}
      >
        {/* Bubble */}
        <div
          style={{
            padding: "0.75rem 1rem",
            borderRadius: isUser ? "1rem 0.25rem 1rem 1rem" : "0.25rem 1rem 1rem 1rem",
            background: isUser ? "rgba(0,200,240,0.1)" : "var(--bg-card)",
            border: isUser
              ? "1px solid rgba(0,200,240,0.2)"
              : msg.error
              ? "1px solid rgba(239,68,68,0.3)"
              : "1px solid var(--border-dim)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: "0.875rem",
            lineHeight: 1.65,
            color: msg.error ? "var(--accent-red)" : "var(--text-primary)",
            maxWidth: "100%",
          }}
        >
          {msg.content}
        </div>

        {/* Candidate grid (assistant only) */}
        {!isUser && msg.candidates && msg.candidates.length > 0 && (
          <div
            style={{
              width: "100%",
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
              gap: "0.75rem",
            }}
          >
            {msg.candidates.map((c, i) => (
              <CandidateCard key={`${c.mpn}-${i}`} c={c} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

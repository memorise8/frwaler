"use client";
import { ReactNode } from "react";

interface TooltipProps {
  text: string;
  children: ReactNode;
}

export default function Tooltip({ text, children }: TooltipProps) {
  return (
    <span
      style={{ position: "relative", display: "inline-block" }}
      className="group"
    >
      {children}
      <span
        style={{
          position: "absolute",
          bottom: "calc(100% + 6px)",
          left: "50%",
          transform: "translateX(-50%)",
          background: "var(--bg-panel)",
          border: "1px solid var(--border-bright)",
          color: "var(--text-secondary)",
          fontSize: "0.72rem",
          lineHeight: 1.5,
          padding: "0.4rem 0.65rem",
          borderRadius: "0.5rem",
          whiteSpace: "normal",
          width: "220px",
          zIndex: 50,
          pointerEvents: "none",
          opacity: 0,
          transition: "opacity 0.15s ease",
          boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
        }}
        className="group-hover:opacity-100"
        role="tooltip"
      >
        {text}
        {/* Arrow */}
        <span
          style={{
            position: "absolute",
            top: "100%",
            left: "50%",
            transform: "translateX(-50%)",
            borderWidth: "5px",
            borderStyle: "solid",
            borderColor:
              "var(--border-bright) transparent transparent transparent",
          }}
        />
      </span>
    </span>
  );
}

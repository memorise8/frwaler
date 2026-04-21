"use client";
import { useRef, useState, DragEvent, ChangeEvent } from "react";

interface DatasheetUploadProps {
  onFile: (file: File) => void;
  partLabel?: string;
}

export default function DatasheetUpload({ onFile, partLabel = "트랜지스터" }: DatasheetUploadProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);

  const handleFile = (file: File | null | undefined) => {
    if (!file) return;
    setFileName(file.name);
    onFile(file);
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    handleFile(file);
  };

  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(true);
  };

  const onDragLeave = () => setDragging(false);

  const onInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    handleFile(e.target.files?.[0]);
  };

  return (
    <div
      className="card"
      onClick={() => inputRef.current?.click()}
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      style={{
        padding: "2rem 1.5rem",
        cursor: "pointer",
        textAlign: "center",
        border: dragging
          ? "1px solid var(--accent-cyan)"
          : fileName
          ? "1px solid rgba(16,185,129,0.4)"
          : "1px dashed var(--border-dim)",
        background: dragging
          ? "var(--accent-cyan-glow)"
          : fileName
          ? "rgba(16,185,129,0.05)"
          : undefined,
        transition: "all 0.2s ease",
        userSelect: "none",
      }}
      role="button"
      aria-label="데이터시트 PDF 업로드"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,application/pdf"
        style={{ display: "none" }}
        onChange={onInputChange}
      />

      {/* Upload icon */}
      <div style={{ marginBottom: "0.875rem" }}>
        {fileName ? (
          <svg
            width="40"
            height="40"
            viewBox="0 0 40 40"
            fill="none"
            style={{ margin: "0 auto" }}
          >
            <circle cx="20" cy="20" r="19" stroke="rgba(16,185,129,0.4)" strokeWidth="1.5" />
            <path d="M12 21l6 6 10-12" stroke="#10b981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <svg
            width="40"
            height="40"
            viewBox="0 0 40 40"
            fill="none"
            style={{ margin: "0 auto" }}
          >
            <circle
              cx="20"
              cy="20"
              r="19"
              stroke={dragging ? "var(--accent-cyan)" : "var(--border-dim)"}
              strokeWidth="1.5"
            />
            <path
              d="M20 27V15M14 21l6-6 6 6"
              stroke={dragging ? "var(--accent-cyan)" : "var(--text-dim)"}
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path
              d="M13 29h14"
              stroke={dragging ? "var(--accent-cyan)" : "var(--border-dim)"}
              strokeWidth="1.8"
              strokeLinecap="round"
            />
          </svg>
        )}
      </div>

      {fileName ? (
        <div>
          <p
            style={{
              color: "#10b981",
              fontWeight: 600,
              fontSize: "0.875rem",
              marginBottom: "0.25rem",
            }}
          >
            파일 선택됨
          </p>
          <p
            style={{
              fontFamily: "'DM Mono', monospace",
              fontSize: "0.775rem",
              color: "var(--text-secondary)",
              wordBreak: "break-all",
            }}
          >
            {fileName}
          </p>
          <p
            style={{
              fontSize: "0.7rem",
              color: "var(--text-dim)",
              marginTop: "0.5rem",
            }}
          >
            클릭하여 다른 파일 선택
          </p>
        </div>
      ) : (
        <div>
          <p
            style={{
              color: "var(--text-primary)",
              fontWeight: 600,
              fontSize: "0.875rem",
              marginBottom: "0.35rem",
            }}
          >
            데이터시트 PDF 드래그 또는 클릭
          </p>
          <p
            style={{
              fontSize: "0.775rem",
              color: "var(--text-secondary)",
              marginBottom: "0.5rem",
            }}
          >
            {partLabel} 규격서(데이터시트)를 업로드하면
            <br />
            AI가 자동으로 파라미터를 추출하여 분석합니다
          </p>
          <span className="chip chip-neutral" style={{ fontSize: "0.7rem" }}>
            PDF 파일만 지원
          </span>
        </div>
      )}
    </div>
  );
}

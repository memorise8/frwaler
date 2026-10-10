"use client";
import Link from "next/link";
import { useState } from "react";
import { fileErrorLabel, filesActive, type FileSummary } from "@/lib/file-jobs";

export default function FileProgress({ jobId, metadataStatus, files, onChange }: {
  jobId: number; metadataStatus: string; files: FileSummary; onChange: () => void;
}) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const terminal = ["done", "failed"].includes(metadataStatus);
  const act = async (action: "retry" | "cancel") => {
    setBusy(true); setMessage("");
    try {
      const response = await fetch(`/api/jobs/${jobId}/files/${action}`, { method: "POST" });
      if (!response.ok) throw new Error();
      const result = await response.json() as { changed: number };
      setMessage(`${result.changed}건의 파일 작업을 ${action === "retry" ? "재등록" : "중지"}했습니다.`);
      onChange();
    } catch { setMessage("파일 작업을 처리하지 못했습니다. 다시 시도하세요."); }
    finally { setBusy(false); }
  };
  return <section className="file-progress" aria-label="PDF 및 텍스트 처리">
    <h3>PDF 다운로드 · 텍스트 추출</h3>
    <p>대상 {files.total}건 · PDF 확보 {files.pdf_done}건 · 텍스트 확보 {files.text_done}건</p>
    <p>대기 {files.queued} · 처리 중 {files.running} · 완료 {files.done} · 실패 {files.failed} · PDF 주소 없음 {files.skipped} · 중지 {files.cancelled}</p>
    {Boolean(files.text_quality_review) && <p role="status">문자 손상 등으로 원문 대조가 필요한 추출문 {files.text_quality_review}건이 있습니다. 텍스트 확보 건수와 내용 품질은 별도로 확인해야 합니다.</p>}
    {!terminal && <p>문서 수집이 끝난 후 파일 처리를 시작합니다. 수집을 취소하면 대기 중인 파일 처리도 중지됩니다.</p>}
    {terminal && files.total === 0 && <p>이번 수집에서 파일 처리 대상으로 등록한 문서가 없습니다.</p>}
    {terminal && filesActive(files) && !files.worker_alive && <p role="status">파일 처리 워커의 응답이 없습니다. 워커가 실행되면 대기 작업을 처리합니다.</p>}
    <p>PDF 주소가 있는 문서만 다운로드합니다. 스캔본의 OCR은 포함되지 않습니다. 기존 파일은 확인 후 재사용합니다.</p>
    {terminal && <div className="file-progress-actions">
      {(files.failed + files.cancelled > 0) && <button disabled={busy} onClick={() => void act("retry")}>실패·중지한 파일 재시도</button>}
      {filesActive(files) && <button disabled={busy} onClick={() => void act("cancel")}>파일 처리 중지</button>}
    </div>}
    {message && <p role="status">{message}</p>}
    {files.items.length > 0 && <details><summary>최근 파일 처리 결과 · 실패 우선 최대 20건</summary>
      <ul>{files.items.map(item => <li key={item.seq_id}><Link href={`/documents/${item.seq_id}`}>문서 #{item.seq_id}</Link>{" · "}
        PDF {item.pdf_status === "done" ? "확보" : item.pdf_status === "unavailable" ? "주소 없음" : item.pdf_status === "failed" ? "실패" : "대기"}
        {" · "}텍스트 {item.text_status === "done" ? "확보" : item.text_status === "failed" ? "실패" : item.text_status === "skipped" ? "건너뜀" : "대기"}
        {item.text_quality?.state === "review" && " · 문자 품질 검토 필요"}
        {item.error && ` · ${fileErrorLabel(item.error)}`}</li>)}</ul>
    </details>}
  </section>;
}

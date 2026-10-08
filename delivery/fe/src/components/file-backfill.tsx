"use client";
import { useState } from "react";

type Selection = {selected: number; next_after_seq: number; job_id: number | null; has_more_unverified: boolean};
export default function FileBackfill({siteId}: {siteId: string}) {
  const [limit, setLimit] = useState(100);
  const [after, setAfter] = useState(0);
  const [recheck, setRecheck] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [preview, setPreview] = useState<Selection | null>(null);
  const [result, setResult] = useState<Selection | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const valid = Number.isInteger(limit) && limit >= 1 && limit <= 1000 && Number.isSafeInteger(after) && after >= 0 && (!from || !to || from <= to);
  async function run(dryRun: boolean) {
    if (!valid || busy || (!dryRun && !preview)) return;
    setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/files/backfill", {method:"POST", headers:{"content-type":"application/json"},
        body:JSON.stringify({site_id:siteId, after_seq:after, limit_n:limit, include_existing:recheck, dry_run:dryRun,
          collected_from:from || null, collected_to:to || null})});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "대상 조건을 확인해 주세요.");
      if (dryRun) { setPreview(data); setResult(null); }
      else { setResult(data); setPreview(null); setMessage(data.job_id ? `파일 작업 #${data.job_id}: ${data.selected}건 등록. 작업 현황에서 PDF·텍스트 결과를 확인하세요.` : "등록할 대상이 없습니다."); }
    } catch (error) { setPreview(null); setMessage(error instanceof Error ? error.message : "요청 실패"); }
    finally { setBusy(false); }
  }
  return <div className="file-progress">
    <h3>기존 문서 PDF·텍스트 이어받기</h3>
    <p>문서 정보를 다시 수집하지 않고 저장된 문서의 파일만 처리합니다. 기본 대상은 DB에 파일 미완료로 기록된 문서입니다.</p>
    <div className="run-fields">
      <label>최대 처리 문서<input type="number" min="1" max="1000" value={limit} disabled={busy} onChange={event => {setLimit(Number(event.target.value)); setPreview(null); setResult(null);}} /></label>
      <label>이 문서 번호 다음부터<input type="number" min="0" value={after} disabled={busy} onChange={event => {setAfter(Number(event.target.value)); setPreview(null); setResult(null);}} /></label>
      <label>DB 수집 시작일 (UTC)<input type="date" value={from} disabled={busy} onChange={event => {setFrom(event.target.value); setPreview(null); setResult(null);}} /></label>
      <label>DB 수집 종료일 (UTC)<input type="date" value={to} disabled={busy} onChange={event => {setTo(event.target.value); setPreview(null); setResult(null);}} /></label>
    </div>
    <label><input type="checkbox" checked={recheck} disabled={busy} onChange={event => {setRecheck(event.target.checked); setPreview(null); setResult(null);}} />완료 표시 문서도 실제 파일 재확인</label>
    <p>재확인은 선택 범위 내 파일을 검사합니다. 이미 대기·실행 중인 문서는 제외하며, 실패 작업은 해당 작업에서 재시도할 수 있습니다.</p>
    <button type="button" disabled={busy || !valid} onClick={() => void run(true)}>대상 미리보기</button>
    {preview && <p>현재 선택 대상 {preview.selected}건. 등록 시 대상 상태를 다시 확인합니다. <button type="button" disabled={busy || !preview.selected} onClick={() => void run(false)}>파일 처리 등록</button></p>}
    {message && <p role="status">{message}</p>}
    {result?.has_more_unverified && <button type="button" disabled={busy} onClick={() => {setAfter(result.next_after_seq); setResult(null); setPreview(null);}}>다음 범위 선택</button>}
  </div>;
}

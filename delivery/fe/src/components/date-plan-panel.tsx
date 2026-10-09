"use client";
import { useState } from "react";
import { readDatePlan, type DatePlan } from "@/lib/date-plan";

export default function DatePlanPanel() {
  const [plan, setPlan] = useState<DatePlan | null>(null);
  const [observation, setObservation] = useState("");
  const [trial, setTrial] = useState(false), [files, setFiles] = useState(false);
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  async function load(file?: File) {
    setPlan(null); setTrial(false); setObservation(""); setMessage("");
    if (!file) return;
    setBusy(true);
    try {
      if (file.size > 5_000_000) throw new Error("계획 파일은 5MB 이하여야 합니다.");
      const bytes = await file.arrayBuffer();
      const parsed = readDatePlan(JSON.parse(new TextDecoder().decode(bytes)));
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      setObservation(Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join(""));
      setPlan(parsed);
    } catch (error) { setMessage(error instanceof Error ? error.message : "계획을 읽을 수 없습니다."); }
    finally { setBusy(false); }
  }
  async function submit() {
    if (!plan || !trial || busy) return;
    setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/jobs/date-plan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_id: "federalregister-gov-documents", lower: plan.lower, upper: plan.upper,
          ranges: plan.ranges, observation_id: observation, limit_n: 100, process_files: files, trial }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? "범위 작업을 등록하지 못했습니다.");
      setMessage(`작업 #${result.id} 등록 완료. 같은 계획으로 범위를 이어받습니다.`);
      setTrial(false);
    } catch (error) { setMessage(error instanceof Error ? error.message : "작업 연결 오류입니다."); }
    finally { setBusy(false); }
  }
  return <section className="catalogue-section" aria-label="날짜 범위 수집">
    <h2>관측한 날짜 범위 이어받기</h2>
    <p>날짜별 총량 관측 계획을 불러온 뒤 범위와 건수를 확인하세요. 같은 계획 파일은 기존 진도에서 이어받습니다.</p>
    <label>총량 관측 계획 파일 <input type="file" accept="application/json,.json" disabled={busy} onChange={event => void load(event.target.files?.[0])} /></label>
    {plan && <div>
      <p>{plan.lower} ~ {plan.upper} · {plan.ranges.length.toLocaleString("ko-KR")}개 구간 · 관측 합계 {plan.total.toLocaleString("ko-KR")}건</p>
      <p>관측 합계와 실제 신규 저장 건수는 다릅니다. 날짜 없는 원문과 수집기 제외 항목, 파일 완전성은 별도 검증이 필요합니다.</p>
      <p>한 작업당 신규 100건까지 저장한 뒤 같은 범위의 다음 작업을 자동 등록합니다. 취소하면 자동 진행이 멈춥니다.</p>
      <label><input type="checkbox" checked={files} disabled={busy} onChange={e => setFiles(e.target.checked)} />PDF·텍스트 처리도 요청</label>
      <label><input type="checkbox" checked={trial} disabled={busy} onChange={e => setTrial(e.target.checked)} />범위와 관측 합계를 확인했으며 시험 수집으로 실행합니다.</label>
      <button type="button" disabled={!trial || busy} onClick={() => void submit()}>지정 범위 수집 시작</button>
    </div>}
    {message && <p role="status">{message}</p>}
  </section>;
}

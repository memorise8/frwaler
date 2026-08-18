"use client";

import { useCallback, useEffect, useState } from "react";
import { jobStatusLabel } from "@/lib/job-status";
import { formatJobDuration } from "@/lib/job-duration";
import { parseQueueSummary, type QueueProgress } from "@/lib/queue-progress";

type Job = { id: number; site_id: string; status: string; saved_count: number; error?: string | null; created_at: string; started_at?: string | null; finished_at?: string | null; logs?:{event:string;message:string;created_at:string}[] };

export function JobDashboard() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [progress, setProgress] = useState<QueueProgress | null>(null);
  const [message, setMessage] = useState(""),[selected,setSelected]=useState<Job|null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs?limit=20", { cache: "no-store" });
      if (!response.ok) throw new Error("load failed");
      setJobs(((await response.json()) as { jobs?: Job[] }).jobs ?? []);
      setMessage("");
    } catch { setMessage("작업 목록을 불러올 수 없습니다."); }
    // Fetched alongside the list but failing independently: losing the
    // progress bar must not also blank the job table.
    try {
      const summary = await fetch("/api/jobs/summary", { cache: "no-store" });
      setProgress(summary.ok ? parseQueueSummary(await summary.json()) : null);
    } catch { setProgress(null); }
  }, []);
  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 10_000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [load]);
  const act = async (job: Job, action: "cancel" | "retry") => {
    const response = await fetch(`/api/jobs/${job.id}/${action}`, { method: "POST" });
    if (!response.ok) setMessage(`${job.id}번 작업을 처리하지 못했습니다.`);
    await load();
  };
  const detail=async(job:Job)=>{try{const response=await fetch(`/api/jobs/${job.id}`,{cache:"no-store"});if(!response.ok)throw new Error();setSelected(await response.json() as Job);}catch{setMessage("작업 상세를 불러올 수 없습니다.");}};
  return <section className="catalogue-section" aria-label="최근 수집 작업" id="jobs">
    <div className="section-heading"><div><p className="eyebrow">CRAWL JOBS</p><h2>최근 작업 현황</h2></div><button type="button" onClick={() => void load()}>새로고침</button></div>
    {message && <aside className="snapshot-note snapshot-note--unavailable"><strong>연결 안내</strong><span>{message}</span></aside>}
    {progress && (
      <div className="queue-progress" role="status" aria-live="polite">
        <div className="queue-progress-line">
          <strong>{progress.headline}</strong>
          {progress.etaText && <span className="queue-progress-eta">{progress.etaText}</span>}
        </div>
        {progress.percent !== null && (
          <div className="queue-progress-track">
            <div className="queue-progress-fill" style={{ width: `${progress.percent}%` }} />
          </div>
        )}
        {progress.active > 0 && progress.etaText === null && (
          <p className="queue-progress-note">
            완료된 작업이 아직 적어 남은 시간을 추정하지 않습니다.
          </p>
        )}
      </div>
    )}
    {jobs.length > 0 ? <div className="table-shell"><table><thead><tr><th>ID</th><th>수집기</th><th>상태</th><th className="number" title="DB에 새로 추가된 문서 수입니다. 이미 수집된 문서만 재처리한 경우 0으로 표시될 수 있으며, 이는 실패가 아닙니다.">신규 저장</th><th title="워커가 실제로 크롤러를 돌린 시간입니다. 신규 0건인데 오래 걸렸다면 작업 번호를 눌러 크롤러 기록을 확인하세요.">소요</th><th>실패 사유</th><th>관리</th></tr></thead><tbody>{jobs.map((job) => <tr key={job.id}><td><button onClick={()=>void detail(job)}>#{job.id}</button></td><td><code>{job.site_id}</code></td><td>{jobStatusLabel(job.status)}</td><td className="number">{job.saved_count}</td><td>{formatJobDuration(job.started_at, job.finished_at) ?? "-"}</td><td>{job.error ?? "-"}</td><td>{["queued", "running"].includes(job.status) ? <button onClick={() => void act(job, "cancel")}>취소</button> : ["failed", "cancelled"].includes(job.status) ? <button onClick={() => void act(job, "retry")}>재시도</button> : "-"}</td></tr>)}</tbody></table></div> : !message && <div className="catalogue-prompt"><div><strong>등록된 작업이 없습니다.</strong><p>위에서 사이트를 검색해 선택하면 바로 수집을 시작할 수 있습니다.</p></div></div>}
    {selected&&<aside className="job-log"><div><strong>작업 #{selected.id} 기록</strong><button onClick={()=>setSelected(null)}>닫기</button></div>{selected.logs?.map((log,index)=><p className={log.event==="crawler_output"?"job-log-crawler":undefined} key={`${log.created_at}-${index}`}><time>{log.event==="crawler_output"?"크롤러":new Date(log.created_at).toLocaleTimeString("ko-KR")}</time><span>{log.message}</span></p>)}</aside>}
  </section>;
}

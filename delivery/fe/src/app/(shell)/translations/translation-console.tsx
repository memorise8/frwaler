"use client";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { requestedPreviewScope } from "@/lib/preview-scope";
import { detectConfigurationFailures } from "@/lib/translation-job-failures";

type Job={id:number;seq_id:number;source_field:string;task_type:string;source_lang:string;provider:string;model_version:string;status:string;attempts:number;max_attempts:number;error_code?:string|null;created_at:string};
type JobsResponse={jobs:Job[];summary:Record<string,number>};
type Preview={counts:Record<string,number>;total:number;estimated_input_chars:number;estimated_prompt_tokens:number;estimated_seconds:number;safety:{open:boolean;reason_codes:string[]}};
const STATUS:Record<string,string>={pending:"대기",running:"실행 중",completed:"완료",failed:"실패",skipped:"건너뜀",cancelled:"취소"};
const TASK_LABEL:Record<string,string>={title_translation:"제목",abstract_summary:"요약"};

export function TranslationConsole(){
  const [jobs,setJobs]=useState<Job[]>([]),[summary,setSummary]=useState<Record<string,number>>({});
  const [message,setMessage]=useState(""),[preview,setPreview]=useState<Preview|null>(null),[previewLimit,setPreviewLimit]=useState(0),[busy,setBusy]=useState(false);
  const load=useCallback(async()=>{try{const r=await fetch("/api/translation?limit=50",{cache:"no-store"});if(!r.ok)throw new Error();const b=await r.json() as JobsResponse;setJobs(b.jobs);setSummary(b.summary);setMessage("");}catch{setMessage("번역 작업 현황을 불러올 수 없습니다.");}},[]);
  useEffect(()=>{const start=setTimeout(()=>void load(),0),timer=setInterval(()=>void load(),10000);return()=>{clearTimeout(start);clearInterval(timer);};},[load]);
  const configFailure=detectConfigurationFailures(jobs);
  const body=(form:HTMLFormElement)=>{const d=new FormData(form);return{tasks:d.getAll("tasks"),lang:d.get("lang")||null,site_id:d.get("site_id")||null,limit:Number(d.get("limit"))};};
  const submit=async(event:FormEvent<HTMLFormElement>,action:"preview"|"enqueue")=>{event.preventDefault();setBusy(true);setMessage("");try{const payload=body(event.currentTarget);const r=await fetch(`/api/translation?action=${action}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const b=await r.json();if(!r.ok)throw new Error();if(action==="preview"){setPreview(b as Preview);setPreviewLimit(payload.limit);}else{setMessage(`${b.created.toLocaleString("ko-KR")}개 작업을 배치 #${b.batch_id}로 등록만 했습니다(처리 결과 아님). 중복 ${b.existing.toLocaleString("ko-KR")}개는 제외했습니다. 처리 결과는 아래 작업 현황에서 확인하세요.`);await load();}}catch{setMessage("안전장치가 실행을 차단했거나 요청을 처리하지 못했습니다. 배치 안전 현황과 Provider 설정을 확인해 주세요.");}finally{setBusy(false);}};
  const act=async(job:Job,action:"cancel"|"retry")=>{const r=await fetch(`/api/translation/${job.id}/${action}`,{method:"POST"});if(!r.ok)setMessage(`${job.id}번 작업을 처리하지 못했습니다.`);await load();};
  return <>
    <section className="translation-config"><div className="section-heading"><div><p className="eyebrow">NEW BATCH</p><h2>제목 번역·한국어 요약 설정</h2></div>{preview&&<p>이번 요청 {requestedPreviewScope(preview.total,previewLimit).toLocaleString("ko-KR")}건 기준 약 {preview.estimated_prompt_tokens.toLocaleString("ko-KR")} tokens · {Math.ceil(preview.estimated_seconds/60)}분 · 전체 대상 {Object.entries(preview.counts).map(([task,count])=>`${TASK_LABEL[task]??task} ${count.toLocaleString("ko-KR")}`).join(" · ")}건</p>}</div>
      <form onSubmit={(e)=>void submit(e,"enqueue")}>
        <fieldset><legend>처리 작업</legend><label><input type="checkbox" name="tasks" value="title_translation" defaultChecked/> 제목 전체 번역</label><label><input type="checkbox" name="tasks" value="abstract_summary" defaultChecked/> 초록 한국어 요약</label></fieldset>
        <div className="fixed-model"><span>처리 방식</span><strong>내부 provider 고정</strong><small>provider·모델·프롬프트 버전은 서버 환경변수로 고정되며 요청에서 바꿀 수 없습니다. 실제 동작 여부는 아래 작업 현황에서 확인하세요.</small></div>
        <label><span>원문 언어</span><input name="lang" placeholder="전체 또는 en"/></label>
        <label><span>사이트 ID</span><input name="site_id" placeholder="전체 사이트"/></label>
        <label><span>최대 작업 수</span><input name="limit" type="number" min="1" max="1000" defaultValue="20"/></label>
        <div className="translation-actions"><button disabled={busy} type="button" onClick={(e)=>void submit({preventDefault:()=>{},currentTarget:e.currentTarget.form!} as unknown as FormEvent<HTMLFormElement>,"preview")}>대상 미리보기</button><button disabled={busy} type="submit">작업 등록</button></div>
      </form>
    </section>
    {configFailure.hasConfigurationFailures&&<aside className="snapshot-note snapshot-note--unavailable"><strong>설정 오류로 실패한 작업 {configFailure.failingCount}건</strong><span>Provider 환경변수(endpoint·모델)가 서버에 구성되지 않아 실패했습니다. 같은 provider·모델·프롬프트로 등록하는 작업은 환경변수를 고치기 전까지 동일하게 실패합니다.</span></aside>}
    {message&&<aside className="snapshot-note"><strong>작업 안내</strong><span>{message}</span></aside>}
    <section className="translation-jobs"><div className="section-heading"><div><p className="eyebrow">QUEUE STATUS</p><h2>번역 작업 현황</h2></div><button onClick={()=>void load()}>새로고침</button></div>
      <div className="translation-summary">{Object.entries(STATUS).map(([key,label])=><div key={key}><span>{label}</span><strong>{(summary[key]??0).toLocaleString("ko-KR")}</strong></div>)}</div>
      {jobs.length?<div className="table-shell"><table><thead><tr><th>ID</th><th>문서</th><th>작업·언어</th><th>Provider·모델</th><th>상태</th><th>시도</th><th>오류</th><th>관리</th></tr></thead><tbody>{jobs.map(job=><tr key={job.id}><td>{job.id}</td><td><a href={`/documents/${job.seq_id}`}>{job.seq_id}</a></td><td>{job.task_type==="summarize"?"한국어 요약":"제목 번역"} · {job.source_lang}</td><td>{job.provider} · <code>{job.model_version}</code></td><td><span className={`translation-state translation-state--${job.status}`}>{STATUS[job.status]??job.status}</span></td><td>{job.attempts}/{job.max_attempts}</td><td>{job.error_code??"-"}</td><td>{job.status==="pending"?<button onClick={()=>void act(job,"cancel")}>취소</button>:job.status==="failed"&&job.attempts<job.max_attempts?<button onClick={()=>void act(job,"retry")}>재시도</button>:"-"}</td></tr>)}</tbody></table></div>:<div className="document-empty"><strong>등록된 요약·번역 작업이 없습니다.</strong><p>대상을 미리 확인한 후 소량 작업부터 등록해 주세요.</p></div>}
    </section>
  </>;
}

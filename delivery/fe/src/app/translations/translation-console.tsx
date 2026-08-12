"use client";
import { FormEvent, useCallback, useEffect, useState } from "react";

type Job={id:number;seq_id:number;source_field:string;source_lang:string;provider:string;model_version:string;status:string;attempts:number;max_attempts:number;error_code?:string|null;created_at:string};
type JobsResponse={jobs:Job[];summary:Record<string,number>};
const STATUS:Record<string,string>={pending:"대기",running:"실행 중",completed:"완료",failed:"실패",skipped:"건너뜀",cancelled:"취소"};

export function TranslationConsole(){
  const [jobs,setJobs]=useState<Job[]>([]),[summary,setSummary]=useState<Record<string,number>>({});
  const [message,setMessage]=useState(""),[preview,setPreview]=useState<number|null>(null),[busy,setBusy]=useState(false);
  const load=useCallback(async()=>{try{const r=await fetch("/api/translation?limit=50",{cache:"no-store"});if(!r.ok)throw new Error();const b=await r.json() as JobsResponse;setJobs(b.jobs);setSummary(b.summary);setMessage("");}catch{setMessage("번역 작업 현황을 불러올 수 없습니다.");}},[]);
  useEffect(()=>{const start=setTimeout(()=>void load(),0),timer=setInterval(()=>void load(),10000);return()=>{clearTimeout(start);clearInterval(timer);};},[load]);
  const body=(form:HTMLFormElement)=>{const d=new FormData(form);return{fields:d.getAll("fields"),provider:d.get("provider"),model_version:d.get("model_version"),prompt_version:"translate-ko-v1",lang:d.get("lang")||null,site_id:d.get("site_id")||null,limit:Number(d.get("limit")),requested_by:"delivery-console"};};
  const submit=async(event:FormEvent<HTMLFormElement>,action:"preview"|"enqueue")=>{event.preventDefault();setBusy(true);setMessage("");try{const payload=body(event.currentTarget);const r=await fetch(`/api/translation?action=${action}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const b=await r.json();if(!r.ok)throw new Error();if(action==="preview")setPreview(b.total as number);else{setMessage(`${b.created.toLocaleString("ko-KR")}개 작업을 등록했습니다. 중복 ${b.existing.toLocaleString("ko-KR")}개는 제외했습니다.`);await load();}}catch{setMessage("요청을 처리하지 못했습니다. 입력값과 Provider 설정을 확인해 주세요.");}finally{setBusy(false);}};
  const act=async(job:Job,action:"cancel"|"retry")=>{const r=await fetch(`/api/translation/${job.id}/${action}`,{method:"POST"});if(!r.ok)setMessage(`${job.id}번 작업을 처리하지 못했습니다.`);await load();};
  return <>
    <section className="translation-config"><div className="section-heading"><div><p className="eyebrow">NEW BATCH</p><h2>번역 대상 설정</h2></div>{preview!==null&&<p>예상 대상 {preview.toLocaleString("ko-KR")}건</p>}</div>
      <form onSubmit={(e)=>void submit(e,"enqueue")}>
        <fieldset><legend>번역 필드</legend><label><input type="checkbox" name="fields" value="title" defaultChecked/> 제목</label><label><input type="checkbox" name="fields" value="description" defaultChecked/> 초록</label></fieldset>
        <label><span>처리 방식</span><select name="provider"><option value="internal">내부 모델</option><option value="external">외부 API</option></select></label>
        <label><span>모델 식별자</span><input name="model_version" required placeholder="예: qwen3:8b"/></label>
        <label><span>원문 언어</span><input name="lang" placeholder="전체 또는 en"/></label>
        <label><span>사이트 ID</span><input name="site_id" placeholder="전체 사이트"/></label>
        <label><span>최대 작업 수</span><input name="limit" type="number" min="1" max="1000" defaultValue="20"/></label>
        <div className="translation-actions"><button disabled={busy} type="button" onClick={(e)=>void submit({preventDefault:()=>{},currentTarget:e.currentTarget.form!} as unknown as FormEvent<HTMLFormElement>,"preview")}>대상 미리보기</button><button disabled={busy} type="submit">작업 등록</button></div>
      </form>
    </section>
    {message&&<aside className="snapshot-note"><strong>작업 안내</strong><span>{message}</span></aside>}
    <section className="translation-jobs"><div className="section-heading"><div><p className="eyebrow">QUEUE STATUS</p><h2>번역 작업 현황</h2></div><button onClick={()=>void load()}>새로고침</button></div>
      <div className="translation-summary">{Object.entries(STATUS).map(([key,label])=><div key={key}><span>{label}</span><strong>{(summary[key]??0).toLocaleString("ko-KR")}</strong></div>)}</div>
      {jobs.length?<div className="table-shell"><table><thead><tr><th>ID</th><th>문서</th><th>필드·언어</th><th>Provider·모델</th><th>상태</th><th>시도</th><th>오류</th><th>관리</th></tr></thead><tbody>{jobs.map(job=><tr key={job.id}><td>{job.id}</td><td><a href={`/documents/${job.seq_id}`}>{job.seq_id}</a></td><td>{job.source_field} · {job.source_lang}</td><td>{job.provider} · <code>{job.model_version}</code></td><td><span className={`translation-state translation-state--${job.status}`}>{STATUS[job.status]??job.status}</span></td><td>{job.attempts}/{job.max_attempts}</td><td>{job.error_code??"-"}</td><td>{job.status==="pending"?<button onClick={()=>void act(job,"cancel")}>취소</button>:job.status==="failed"&&job.attempts<job.max_attempts?<button onClick={()=>void act(job,"retry")}>재시도</button>:"-"}</td></tr>)}</tbody></table></div>:<div className="document-empty"><strong>등록된 번역 작업이 없습니다.</strong><p>대상을 미리 확인한 후 소량 작업부터 등록해 주세요.</p></div>}
    </section>
  </>;
}

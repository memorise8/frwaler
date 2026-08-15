"use client";
import { FormEvent, useState } from "react";
import { parseScheduleLimit } from "@/lib/crawl-limit";
import { formatScheduleTimestamp } from "@/lib/schedule-time";
import { describeScheduleSaveFailure, SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE, type BackendErrorBody } from "@/lib/schedule-save-error";

type Schedule={id:number;site_id:string;interval_hours:number;mode:string;limit_n:number|null;enabled:boolean;next_run_at:string;last_run_at:string|null};
type SiteOption={siteId:string;siteName:string};

export default function ScheduleManager({initialRows,sites}:{initialRows:Schedule[];sites:SiteOption[]}) {
  const [rows,setRows]=useState(initialRows); const [message,setMessage]=useState<{text:string;error:boolean}|null>(null);
  const save=async(siteId:string,body:object)=>{setMessage(null);let response:Response;try{response=await fetch(`/api/schedules/${encodeURIComponent(siteId)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(body)});}catch{setMessage({text:SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE,error:true});return;}let payload:unknown;try{payload=await response.json();}catch{setMessage({text:describeScheduleSaveFailure(response.status,{}),error:true});return;}if(!response.ok){setMessage({text:describeScheduleSaveFailure(response.status,payload as BackendErrorBody),error:true});return;}const row=payload as Schedule;setRows(old=>[...old.filter(item=>item.site_id!==row.site_id),row].sort((a,b)=>a.site_id.localeCompare(b.site_id)));setMessage({text:`${siteId} 예약을 저장했습니다.`,error:false});};
  const submit=(event:FormEvent<HTMLFormElement>)=>{event.preventDefault();const data=new FormData(event.currentTarget);const siteId=String(data.get("site_id")??"").trim();if(!siteId)return;const limitResult=parseScheduleLimit(data.get("limit_n"));if(!limitResult.ok){setMessage({text:limitResult.error,error:true});return;}void save(siteId,{interval_hours:Number(data.get("interval_hours")),mode:"incremental",limit_n:limitResult.limit,enabled:true});event.currentTarget.reset();};
  return <>
    <form className="document-filters" onSubmit={submit}><label><span>사이트 ID</span><input name="site_id" required placeholder="예: government-se-publications" list="site-options" autoComplete="off"/></label><label><span>수집 주기</span><select name="interval_hours" defaultValue="168"><option value="24">매일</option><option value="168">매주</option><option value="720">매월</option></select></label><label><span>1회 수집 제한</span><input name="limit_n" type="number" min="1" max="1000" defaultValue="100" placeholder="예: 100"/></label><button type="submit">예약 저장</button></form>
    <datalist id="site-options">{sites.map(site=><option key={site.siteId} value={site.siteId}>{site.siteName||site.siteId}</option>)}</datalist>
    {message&&<p className={`run-message${message.error?" run-message--error":" run-message--success"}`} role={message.error?"alert":undefined}>{message.text}</p>}
    {rows.length?<div className="table-shell"><table><thead><tr><th>사이트</th><th>주기</th><th>모드·제한</th><th>상태</th><th>다음 실행</th><th>최근 실행</th><th>중지 · 재개</th></tr></thead><tbody>{rows.map(row=><tr key={row.id}><td><code>{row.site_id}</code></td><td>{row.interval_hours}시간</td><td>{row.mode} · {row.limit_n??"제한 없음"}</td><td>{row.enabled?"활성":"중지"}</td><td>{formatScheduleTimestamp(row.next_run_at)}</td><td>{row.last_run_at?formatScheduleTimestamp(row.last_run_at):"아직 없음"}</td><td><button title={row.enabled?"이 예약을 중지합니다. 삭제 기능은 없으며, 재개를 누르기 전까지 다시 실행되지 않습니다.":"이 예약을 다시 시작합니다."} onClick={()=>void save(row.site_id,{interval_hours:row.interval_hours,mode:row.mode,limit_n:row.limit_n,enabled:!row.enabled})}>{row.enabled?"중지":"재개"}</button></td></tr>)}</tbody></table><p className="run-note">예약을 삭제하는 기능은 없습니다. 필요 없는 예약은 중지로 전환하세요. 중지된 예약은 목록에 남아 있으며, 재개를 누르기 전까지 다시 실행되지 않습니다.</p></div>:<div className="document-empty"><strong>등록된 예약이 없습니다.</strong><p>필요한 사이트만 증분 수집하도록 설정할 수 있습니다.</p></div>}
  </>;
}

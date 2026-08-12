"use client";
import { FormEvent, useState } from "react";

type Schedule={id:number;site_id:string;interval_hours:number;mode:string;limit_n:number|null;enabled:boolean;next_run_at:string;last_run_at:string|null};

export default function ScheduleManager({initialRows}:{initialRows:Schedule[]}) {
  const [rows,setRows]=useState(initialRows); const [message,setMessage]=useState("");
  const save=async(siteId:string,body:object)=>{setMessage("");const response=await fetch(`/api/schedules/${encodeURIComponent(siteId)}`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(body)});if(!response.ok){setMessage("예약을 저장하지 못했습니다.");return;}const row=await response.json() as Schedule;setRows(old=>[...old.filter(item=>item.site_id!==row.site_id),row].sort((a,b)=>a.site_id.localeCompare(b.site_id)));setMessage(`${siteId} 예약을 저장했습니다.`);};
  const submit=(event:FormEvent<HTMLFormElement>)=>{event.preventDefault();const data=new FormData(event.currentTarget);const siteId=String(data.get("site_id")??"").trim();const limit=String(data.get("limit_n")??"").trim();if(!siteId)return;void save(siteId,{interval_hours:Number(data.get("interval_hours")),mode:"incremental",limit_n:limit?Number(limit):null,enabled:true});event.currentTarget.reset();};
  return <>
    <form className="document-filters" onSubmit={submit}><label><span>사이트 ID</span><input name="site_id" required placeholder="예: government-se-publications"/></label><label><span>수집 주기</span><select name="interval_hours" defaultValue="168"><option value="24">매일</option><option value="168">매주</option><option value="720">매월</option></select></label><label><span>1회 수집 제한</span><input name="limit_n" type="number" min="1" max="10000" placeholder="제한 없음"/></label><button type="submit">예약 저장</button></form>
    {message&&<p className="run-message">{message}</p>}
    {rows.length?<div className="table-shell"><table><thead><tr><th>사이트</th><th>주기</th><th>모드·제한</th><th>상태</th><th>다음 실행</th><th>최근 실행</th><th>관리</th></tr></thead><tbody>{rows.map(row=><tr key={row.id}><td><code>{row.site_id}</code></td><td>{row.interval_hours}시간</td><td>{row.mode} · {row.limit_n??"제한 없음"}</td><td>{row.enabled?"활성":"중지"}</td><td>{new Date(row.next_run_at).toLocaleString("ko-KR")}</td><td>{row.last_run_at?new Date(row.last_run_at).toLocaleString("ko-KR"):"아직 없음"}</td><td><button onClick={()=>void save(row.site_id,{interval_hours:row.interval_hours,mode:row.mode,limit_n:row.limit_n,enabled:!row.enabled})}>{row.enabled?"중지":"재개"}</button></td></tr>)}</tbody></table></div>:<div className="document-empty"><strong>등록된 예약이 없습니다.</strong><p>필요한 사이트만 증분 수집하도록 설정할 수 있습니다.</p></div>}
  </>;
}

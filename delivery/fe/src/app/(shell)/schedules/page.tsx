import "server-only";
import ScheduleManager from "./schedule-manager";
import { getCrawlerHealth } from "@/lib/crawler-health";

// Every other server-data page carries this; without it `next build` treats
// this route as static and prerenders it. Two things break at once: the
// audited catalogue CSV is not in the FE build stage (only the runtime
// image), so the build fails outright -- and had it succeeded, load() would
// have run against a backend that does not exist during the build, caught its
// own failure, and baked "연결 안 됨" into the page for the life of the image.
export const dynamic = "force-dynamic";

type Schedule={id:number;site_id:string;interval_hours:number;mode:string;limit_n:number|null;enabled:boolean;next_run_at:string;last_run_at:string|null};
const backend=()=>(process.env.BE_URL??"http://127.0.0.1:8080").replace(/\/$/,"");
async function load():Promise<Schedule[]|null>{try{const r=await fetch(`${backend()}/schedules`,{cache:"no-store",signal:AbortSignal.timeout(8000)});if(!r.ok)return null;return ((await r.json()) as {schedules:Schedule[]}).schedules;}catch{return null;}}
export default async function SchedulesPage(){const rows=await load();const sites=getCrawlerHealth().map((row)=>({siteId:row.siteId,siteName:row.siteName}));return <div className="documents-page"><header className="documents-hero"><div><p className="eyebrow">INCREMENTAL SCHEDULES</p><h1>수집 최신화를<br/>예약합니다.</h1></div><p>같은 사이트에 활성 작업이 있으면 중복 등록하지 않습니다. 고객은 사이트와 주기만 정하면 됩니다.</p></header>{rows===null?<aside className="snapshot-note snapshot-note--unavailable"><strong>연결 안 됨</strong><span>예약 정보를 불러올 수 없습니다.</span></aside>:<ScheduleManager initialRows={rows} sites={sites}/>}</div>}

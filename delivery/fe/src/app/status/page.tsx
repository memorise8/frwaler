import Link from "next/link";
import snapshot from "@/content/crawler-checks.json";
import { getLiveCrawlerStatus } from "@/lib/live-crawler-status";
import Refresh from "./refresh";

export const dynamic = "force-dynamic";
type Check = { site_id: string; name: string; status: string; checked_date: string | null; source_file: string; error: string | null; samples: { title: string; url: string }[] };
const labels: Record<string, string> = snapshot.labels;
const resultLabels: Record<string, string> = { unrun: "종료 기록 없음", failed: "최근 실행 실패", collected: "최근 실행에서 저장 확인", empty: "최근 실행 신규 0건 · 원인 미확인", cancelled: "최근 실행 취소" };
const jobLabels: Record<string, string> = { queued: "대기", running: "실행 중", cancelling: "중지 요청", done: "종료", failed: "실패", cancelled: "취소" };
const time = (value?: string | null) => value ? new Date(value).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) : "기록 없음";
const safeUrl = (url: string) => /^https?:\/\//i.test(url) ? url : undefined;

export default async function Status({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const value = (name: string) => typeof params[name] === "string" ? params[name] as string : "";
  const q = value("q"), status = value("status"), execution = value("execution");
  const live = await getLiveCrawlerStatus();
  const runtime = new Map(live.items.map(row => [row.site_id, row]));
  const catalogue = new Map<string, Check>((snapshot.rows as Check[]).map(row => [row.site_id, row]));
  for (const row of live.items) if (!catalogue.has(row.site_id)) catalogue.set(row.site_id, { site_id: row.site_id, name: row.site_id, status: "unverified", checked_date: null, source_file: "", error: null, samples: [] });
  const all = [...catalogue.values()];
  const filtered = all.filter(row => {
    const record = runtime.get(row.site_id);
    return `${row.site_id} ${row.name}`.toLowerCase().includes(q.toLowerCase()) && (!status || row.status === status)
      && (!execution || !live.available || (record?.result_state ?? "unrun") === execution);
  });
  const pages = Math.max(1, Math.ceil(filtered.length / 30));
  const page = Math.min(pages, Math.max(1, Number.parseInt(value("page"), 10) || 1));
  const href = (next: number) => `/status?${new URLSearchParams({ q, status, execution, page: String(next) })}`;
  return <div className="status-page">
    <header><p className="eyebrow">CRAWLER STATUS</p><h1>수집기별 현재 상태</h1><p>최근 운영 실행과 검사 기록을 함께 확인합니다. 표본 저장은 전량 수집 완료를 뜻하지 않습니다.</p></header>
    <aside className="snapshot-note" role="status"><strong>{live.available ? "운영 이력 연결됨" : "운영 상태 조회 불가"}</strong><span>{live.available ? `조회 ${time(live.measured_at)} · 이 화면이 열려 있으면 30초마다 갱신합니다.` : "운영 API에 연결할 수 없습니다. 아래 검사 기록은 조회할 수 있지만 지금 실행 중인지는 확인할 수 없습니다."}</span><Refresh /></aside>
    <p>등록·실행 식별자 {all.length.toLocaleString("ko-KR")}개 · 검사 기록은 2026-09-11 기준이며 당시 코드·환경에서 관측된 결과입니다. 실행 중 표시는 작업 큐 기준이며 프로세스 생존 확인 기능은 아직 연결되지 않았습니다.</p>
    <form className="filters" action="/status">
      <label className="query-field"><span>수집기 검색</span><input name="q" defaultValue={q} placeholder="수집기 ID 또는 기관 이름" /></label>
      <label><span>최근 검사 결과</span><select name="status" defaultValue={status}><option value="">전체</option>{Object.entries(labels).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></label>
      <label><span>최근 운영 결과</span><select name="execution" defaultValue={execution} disabled={!live.available}><option value="">전체</option>{Object.entries(resultLabels).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></label>
      <button>찾기</button><Link href="/status">초기화</Link>
    </form>
    <p>{filtered.length.toLocaleString("ko-KR")}개 결과 · {page}/{pages} 페이지</p>
    <div className="crawler-status-list">{filtered.slice((page-1)*30,page*30).map(row => {
      const record = runtime.get(row.site_id), result = record?.last_result;
      return <article className="crawler-status-card" key={row.site_id}>
        <div className="section-heading"><div><h2>{row.name.replace(/^Custom:\s*/, "")}</h2><code>{row.site_id}</code></div><span>{labels[row.status] ?? row.status}</span></div>
        <p>검사일 {row.checked_date ?? "미검증"} · 저장 표본 {row.samples.length}건</p>
        <p><strong>최근 운영 결과: </strong>{!live.available ? "조회 불가" : record ? resultLabels[record.result_state] ?? "미확인" : "실행 기록 없음"}</p>
        {live.available && record && <><p>최근 작업 #{record.latest_job?.id}: {jobLabels[record.latest_job?.status ?? ""] ?? "미확인"} · 최근 종료 {time(result?.finished_at)} · 신규 저장 {result?.saved_count ?? 0}건</p>
          <p>현재 작업: {Object.entries(record.active_counts).map(([key,count]) => `${jobLabels[key] ?? key} ${count}개`).join(" · ") || "대기·실행 작업 없음"}</p>
          <p>마지막 저장 확인: {time(record.last_success?.finished_at)}{record.last_success ? ` · ${record.last_success.saved_count}건` : ""}</p>
          {result?.error && <pre className="status-error">{result.error}</pre>}</>}
        {row.error && <p className="status-error">검사 오류: {row.error}</p>}
        <details><summary>검사 근거와 문서 표본</summary><code>{row.source_file}</code><ul>{row.samples.map((s,i) => <li key={i}>{safeUrl(s.url) ? <a href={safeUrl(s.url)} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title}</li>)}</ul>{!row.samples.length && <p>이 검사에서는 저장 표본을 확인하지 못했습니다.</p>}</details>
      </article>;
    })}</div>
    {!filtered.length && <p>조건에 맞는 수집기가 없습니다.</p>}
    <nav className="pagination" aria-label="페이지 이동">{page>1 ? <Link href={href(page-1)}>이전</Link> : <span/>}<span>{page}/{pages}</span>{page<pages ? <Link href={href(page+1)}>다음</Link> : <span/>}</nav>
  </div>;
}

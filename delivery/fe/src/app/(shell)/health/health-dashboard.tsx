"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { allSitesUnchecked, formatHealthDate as dateText, healthLabel, healthTone, HEALTH_LABELS, type CrawlerHealthReport, type SystemHealthReport } from "@/lib/health-status";

export default function HealthDashboard({ crawlers, system }: {
  crawlers: CrawlerHealthReport | null; system: SystemHealthReport | null;
}) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const timer = window.setInterval(() => router.refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [router]);
  const rows = (crawlers?.sites ?? []).filter(row => (!state || row.probe.state === state) &&
    `${row.site_name} ${row.site_id}`.toLowerCase().includes(query.toLowerCase()));
  const pages = Math.max(1, Math.ceil(rows.length / 40));
  const currentPage = Math.min(page, pages);
  const visible = rows.slice((currentPage - 1) * 40, currentPage * 40);
  const counts = crawlers?.summary.probes ?? {};
  const nothingChecked = allSitesUnchecked(crawlers?.sites ?? []);
  const showTable = !nothingChecked || expanded;
  const blob = system?.blob_sample;
  const blobText = !blob ? "확인 불가" : blob.state === "no_sample" ? "확인할 PDF 없음" : blob.state === "sample_ok"
    ? `표본 ${blob.checked}개 확인` : `누락 ${blob.missing ?? 0}개 · 형식 오류 ${blob.invalid ?? 0}개`;

  return <div className="status-page health-page">
    <div className="section-heading">
      <div><h1>건강 점검</h1><p>최근 검사 결과와 운영 환경을 확인합니다. 30초마다 표시를 갱신합니다. 시각은 한국 시간입니다.</p></div>
      <button type="button" onClick={() => router.refresh()}>새로고침</button>
    </div>
    <aside className="snapshot-note">
      <strong>검사 범위</strong><span>표본 검사는 사이트에 접속해 제목·링크를 읽는 단계까지 확인합니다.
        실제 문서 저장·PDF 다운로드·전체 페이지 수집 성공을 보장하지 않습니다.
        검사 후 {crawlers?.stale_hours ?? 48}시간이 지나거나 코드가 변경되면 재검사가 필요합니다.</span>
    </aside>
    {!system ? <p role="alert" className="run-message run-message--error">운영 환경을 확인할 수 없습니다. 백엔드 연결·인증 또는 배포 버전을 확인하세요.</p> : <>
      <div className="summary-grid">
        <article className="summary-card"><span>DB·초기화</span><strong>{system.schema === "ready" ? "준비됨" : "점검 필요"}</strong><small>{system.schema === "ready" ? "필수 DB 구조 확인" : "DB 업데이트 필요"}</small></article>
        <article className="summary-card"><span>워커 응답</span><strong>{system.workers.filter(w => w.alive).length} / {system.workers.length}</strong><small>최근 신호 또는 유효한 실행 작업 기준</small></article>
        <article className="summary-card"><span>작업 대기 / 실행</span><strong>{system.queue.queued ?? 0} / {system.queue.running ?? 0}</strong><small>활성 예약 {system.enabled_schedules ?? 0}개</small></article>
      </div>
      <p className="run-note">원본 파일: {blobText}. DB에서 PDF 확보로 표시된 최근 최대 20개를 확인한 결과입니다.</p>
      {system.workers.length === 0 && <p role="alert">워커 응답 기록이 없습니다. 워커가 실행 중인지 확인하세요.</p>}
      {system.workers.map(worker => <p className="run-note" key={worker.worker_id}>
        {worker.worker_id}: {worker.alive ? "응답 확인" : "응답 오래됨"}
        {worker.crawl_started_at && ` · 수집 시작 ${dateText(worker.crawl_started_at)} (진행 여부는 작업 기록 확인)`}
      </p>)}
    </>}
    {!crawlers ? <p role="alert" className="run-message run-message--error">수집기 검사 결과를 불러올 수 없습니다. 연결 상태와 배포 버전을 확인하세요.</p> : <>
      <div className="summary-grid">
        <article className="summary-card"><span>전체 수집기</span><strong>{crawlers.summary.catalogue}</strong><small>코드 로딩 가능 {crawlers.summary.code.ready ?? 0}개</small></article>
        <article className="summary-card"><span>최근 표본 정상</span><strong>{counts.healthy ?? 0}</strong><small>과거 납품 판정과 별도로 집계</small></article>
        <article className="summary-card"><span>미검사</span><strong>{counts.unchecked ?? 0}</strong><small>검사 서비스 실행 후 갱신</small></article>
      </div>
      {crawlers.registry_errors.length > 0 && <p role="alert">수집기 코드 로딩 오류 {crawlers.registry_errors.length}개가 있습니다. 관리자 확인이 필요합니다.</p>}
      {nothingChecked && <aside className="snapshot-note">
        <strong>아직 검사한 사이트가 없습니다</strong>
        <span>{crawlers.sites.length.toLocaleString("ko-KR")}개 사이트 모두 「미검사」입니다. 표본 검사가 한 번도 돌지 않았다는 뜻이며, 고장이라는 뜻은 아닙니다.
          검사 서비스가 돌면 이 자리에 사이트별 결과가 나옵니다.</span>
        <button type="button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>{expanded ? "사이트 목록 접기" : `사이트 목록 펼치기 (${crawlers.sites.length.toLocaleString("ko-KR")}개)`}</button>
      </aside>}
      {showTable && <>
      <div className="filters">
        <label><span>사이트 검색</span><input value={query} onChange={e => {setQuery(e.target.value); setPage(1);}} placeholder="사이트 이름 또는 ID" /></label>
        <label><span>최근 검사 상태</span><select value={state} onChange={e => {setState(e.target.value); setPage(1);}}>
          <option value="">전체 상태</option>{Object.entries(HEALTH_LABELS).map(([key, label]) => <option value={key} key={key}>{label} ({counts[key] ?? 0})</option>)}
        </select></label>
      </div>
      <p className="run-note">{rows.length}개 · 표시 갱신 {dateText(crawlers.measured_at)}. 미검사는 고장을 뜻하지 않습니다.</p>
      <div className="table-shell"><table>
        <thead><tr><th>사이트</th><th>코드 확인</th><th>최근 표본 검사</th><th>검사 시각</th><th>확인 건수</th><th>이어받기 코드</th></tr></thead>
        <tbody>{visible.map(row => <tr key={row.site_id}>
          <td><Link href={`/crawlers/${encodeURIComponent(row.site_id)}`}>{row.site_name}</Link><small className="reason">{row.site_id}</small></td>
          <td>{row.code_status === "ready" ? "로딩 가능" : "점검 필요"}</td>
          <td><span className={`health-badge health-badge--${healthTone(row.probe.state)}`}>{healthLabel(row.probe.state)}</span>
            {row.probe.last_state && <small className="reason">이전: {healthLabel(row.probe.last_state)}</small>}</td>
          <td>{dateText(row.probe.checked_at)}</td><td>{row.probe.sample_count ?? "—"}</td>
          <td>{row.resume_signals ? "구현 신호 있음" : "별도 확인 필요"}</td>
        </tr>)}</tbody>
      </table></div>
      {!visible.length && <p>조건에 맞는 수집기가 없습니다.</p>}
      <div className="section-heading"><button type="button" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>이전</button>
        <span>{currentPage} / {pages}</span><button type="button" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>다음</button></div>
      </>}
    </>}
  </div>;
}

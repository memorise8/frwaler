import Form from "next/form";
import Link from "next/link";
import { AUDIT_DATE, getCrawlerHealth } from "@/lib/crawler-health";
import { getDatabaseStats, getFreshnessStats } from "@/lib/database-stats";
import { getRecentJobs } from "@/lib/crawl-jobs";
import { searchCrawlersToRun } from "@/lib/crawler-search";
import { DEFAULT_QUEUE, isQueueKey, selectFailedCrawlers, selectRecentSites, selectStaleSites, type QueueKey, type QueueRow, type QueueSelection } from "@/lib/collect-queues";
import RunPanel from "@/components/run-panel";
import { JobDashboard } from "./job-dashboard";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 40;
type Params = Promise<Record<string, string | string[] | undefined>>;
const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";

const QUEUE_COPY: Readonly<Record<QueueKey, { label: string; caption: string; heading: string; note: string; tone: string }>> = {
  stale: {
    label: "오래 방치됨",
    caption: "90일 초과·미수집",
    heading: "오래 방치된 사이트",
    note: "마지막 수집이 오래된 순서입니다. 한 번도 수집하지 않은 사이트가 맨 위에 옵니다.",
    tone: " summary-card--bad",
  },
  recent: {
    label: "최근 실행함",
    caption: "최근 작업이 있던 사이트",
    heading: "최근 실행한 사이트",
    note: "가장 최근 작업 순서입니다. 같은 사이트를 여러 번 돌렸어도 한 줄로 묶었습니다.",
    tone: "",
  },
  failed: {
    label: "실패·확인 필요",
    caption: `${AUDIT_DATE} 검증 기준`,
    heading: "실패한 수집기",
    note: "마지막 검증에서 문서를 저장하지 못한 수집기입니다. 다시 실행해도 같은 이유로 실패할 수 있습니다.",
    tone: " summary-card--bad",
  },
};

export default async function Collect({ searchParams }: Readonly<{ searchParams: Params }>) {
  const params = await searchParams;
  const query = one(params.q).trim();
  const requestedSite = one(params.site).trim();
  const requestedQueue = one(params.queue);
  const queue: QueueKey = isQueueKey(requestedQueue) ? requestedQueue : DEFAULT_QUEUE;
  const requestedPage = Number.parseInt(one(params.page), 10) || 1;

  const rows = getCrawlerHealth();
  const [databaseStats, freshnessStats, jobs] = await Promise.all([
    getDatabaseStats(),
    getFreshnessStats(),
    getRecentJobs(),
  ]);
  const documentsBySite = new Map((databaseStats?.by_site ?? []).map((item) => [item.key, item.documents]));

  const queues: Readonly<Record<QueueKey, QueueSelection>> = {
    stale: selectStaleSites(freshnessStats?.sites ?? [], rows, documentsBySite),
    recent: selectRecentSites(jobs, rows, documentsBySite),
    failed: selectFailedCrawlers(rows, documentsBySite),
  };

  const selected = requestedSite ? rows.find((row) => row.siteId === requestedSite) ?? null : null;
  const searching = query !== "";
  const search = searchCrawlersToRun(rows, query);

  const searchRows: readonly QueueRow[] = search.matches.map((row) => ({
    siteId: row.siteId,
    siteName: row.siteName || row.siteId,
    country: row.country,
    documents: documentsBySite.get(row.siteId) ?? null,
    note: row.status === "healthy" ? "마지막 검증 정상" : row.category || "마지막 검증 실패",
  }));

  const active = queues[queue];
  const listRows = searching ? searchRows : active.rows;
  const pageCount = Math.max(1, Math.ceil(listRows.length / PAGE_SIZE));
  const page = Math.min(Math.max(requestedPage, 1), pageCount);
  const visible = searching ? listRows : listRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const selectHref = (siteId: string): string => {
    const next = new URLSearchParams();
    if (query) next.set("q", query);
    else if (queue !== DEFAULT_QUEUE) next.set("queue", queue);
    if (!query && page > 1) next.set("page", String(page));
    next.set("site", siteId);
    return `/?${next}#run-heading`;
  };
  const queueHref = (key: QueueKey): string => key === DEFAULT_QUEUE ? "/" : `/?queue=${key}`;
  const pageHref = (target: number): string => {
    const next = new URLSearchParams();
    if (queue !== DEFAULT_QUEUE) next.set("queue", queue);
    if (target > 1) next.set("page", String(target));
    return next.size ? `/?${next}` : "/";
  };

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">RUN CRAWLER</p>
          <h1>무엇부터,<br />수집할까요?</h1>
          <p className="lede">손봐야 할 사이트를 모아 두었습니다. 카드를 골라 목록에서 사이트를 선택하면 아래에서 곧바로 수집을 시작할 수 있습니다. 처음 다루는 사이트는 3건 상태 확인으로 점검한 뒤 범위를 넓히는 것을 권장합니다.</p>
        </div>
        <div className="audit-date"><span>CRAWLERS</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>정기 실행이 필요하면 <Link href="/schedules">수집 예약</Link></small></div>
      </header>

      <div className="summary-grid">
        {(Object.keys(QUEUE_COPY) as QueueKey[]).map((key) => {
          const copy = QUEUE_COPY[key];
          const isActive = !searching && key === queue;
          return (
            <Link
              aria-current={isActive ? "true" : undefined}
              className={`summary-card${copy.tone}${isActive ? " summary-card--selected" : ""}`}
              href={queueHref(key)}
              key={key}
            >
              <span>{copy.label}</span>
              <strong>{queues[key].rows.length.toLocaleString("ko-KR")}</strong>
              <small>{copy.caption}</small>
            </Link>
          );
        })}
      </div>

      <section className="catalogue-section" aria-labelledby="pick-heading">
        <div className="section-heading">
          <div><p className="eyebrow">SELECT SITE</p><h2 id="pick-heading">{searching ? `"${query}" 검색 결과` : QUEUE_COPY[queue].heading}</h2></div>
          <p>{searching ? `${search.total.toLocaleString("ko-KR")}개 일치` : `${active.rows.length.toLocaleString("ko-KR")}개`}</p>
        </div>

        <Form className="filters filters--run" action="/">
          <label className="query-field"><span>사이트 이름으로 찾기</span><input defaultValue={query} name="q" placeholder="사이트 이름 또는 site_id" /></label>
          <button type="submit">찾기</button>
          {(searching || selected) && <Link className="reset-link" href="/">초기화</Link>}
        </Form>

        <p className="run-note">{searching ? "카드를 누르면 다시 목록으로 돌아갑니다." : QUEUE_COPY[queue].note}</p>

        {!searching && active.excluded > 0 && (
          <aside className="snapshot-note">
            <strong>목록에서 제외</strong>
            <span>{active.excluded.toLocaleString("ko-KR")}개 사이트는 데이터베이스에는 있으나 실행할 수집기가 카탈로그에 없어 목록에서 제외했습니다.</span>
          </aside>
        )}

        {visible.length > 0 ? (
          <div className="table-shell">
            <table>
              <thead><tr><th>사이트</th><th>수집기 ID</th><th>국가</th><th>{searching ? "마지막 검증" : "상태"}</th><th className="number">수집한 문서</th><th>실행</th></tr></thead>
              <tbody>{visible.map((row) => {
                const isSelected = selected?.siteId === row.siteId;
                return (
                  <tr className={isSelected ? "row-selected" : ""} key={row.siteId}>
                    <td className="site-name">{row.siteName}</td>
                    <td><code>{row.siteId}</code></td>
                    <td>{row.country || "-"}</td>
                    <td>{row.note}</td>
                    <td className="number">{row.documents === null ? "-" : row.documents.toLocaleString("ko-KR")}</td>
                    <td>{isSelected ? <span className="success-note">선택됨</span> : <Link className="run-link" href={selectHref(row.siteId)}>선택 →</Link>}</td>
                  </tr>
                );
              })}</tbody>
            </table>
            {searching && search.truncated && <p className="run-note">일치하는 {search.total.toLocaleString("ko-KR")}개 중 {search.matches.length}개만 표시했습니다. 검색어를 더 좁혀 주세요.</p>}
          </div>
        ) : (
          <div className="catalogue-prompt">
            <span aria-hidden="true">↗</span>
            <div>
              <strong>{searching ? `"${query}"와 일치하는 수집기가 없습니다.` : "이 목록은 비어 있습니다."}</strong>
              <p>{searching
                ? <>사이트 이름 또는 site_id의 일부로 검색해 보세요. 국가·자료 유형으로 훑어보려면 <Link href="/crawlers">크롤러 상태</Link>를 이용하세요.</>
                : <>다른 카드를 선택하거나, 사이트 이름으로 직접 검색해 주세요. 전체 목록은 <Link href="/crawlers">크롤러 상태</Link>에 있습니다.</>}</p>
            </div>
          </div>
        )}

        {!searching && pageCount > 1 && (
          <nav className="pagination" aria-label="페이지 이동">
            {page > 1 ? <Link href={pageHref(page - 1)}>← 이전</Link> : <span />}
            <span>{page} / {pageCount}</span>
            {page < pageCount ? <Link href={pageHref(page + 1)}>다음 →</Link> : <span />}
          </nav>
        )}

        {requestedSite && !selected && (
          <aside className="snapshot-note snapshot-note--unavailable">
            <strong>알 수 없는 수집기</strong>
            <span><code>{requestedSite}</code>는 등록된 수집기 카탈로그에 없습니다. 실행 패널을 열지 않았습니다.</span>
          </aside>
        )}
      </section>

      {selected && <>
        <aside className="snapshot-note"><strong>{selected.siteName || selected.siteId}</strong><span>
          {selected.status === "healthy" ? `${AUDIT_DATE} 검증에서 문서 저장에 성공한 수집기입니다.` : `${AUDIT_DATE} 검증에서 실패한 수집기입니다.`}
          {" "}자료 유형 {selected.docType} · 국가 {selected.country} · 현재 DB 문서 {(documentsBySite.get(selected.siteId) ?? 0).toLocaleString("ko-KR")}건 ·{" "}
          <Link href={`/crawlers/${encodeURIComponent(selected.siteId)}`}>수집기 상세 보기</Link>
        </span></aside>
        <RunPanel siteId={selected.siteId} status={selected.status} category={selected.category} reason={selected.reason} />
      </>}

      <JobDashboard />
    </div>
  );
}

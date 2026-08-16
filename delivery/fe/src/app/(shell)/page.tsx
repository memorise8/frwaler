import Form from "next/form";
import Link from "next/link";
import { AUDIT_DATE, getCrawlerHealth } from "@/lib/crawler-health";
import { getDatabaseStats } from "@/lib/database-stats";
import { searchCrawlersToRun } from "@/lib/crawler-search";
import RunPanel from "@/components/run-panel";
import { JobDashboard } from "./job-dashboard";

export const dynamic = "force-dynamic";

type Params = Promise<Record<string, string | string[] | undefined>>;
const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";

export default async function Collect({ searchParams }: Readonly<{ searchParams: Params }>) {
  const params = await searchParams;
  const query = one(params.q).trim();
  const requestedSite = one(params.site).trim();
  const rows = getCrawlerHealth();
  const databaseStats = await getDatabaseStats();
  const documentsBySite = new Map((databaseStats?.by_site ?? []).map((item) => [item.key, item.documents]));
  const selected = requestedSite ? rows.find((row) => row.siteId === requestedSite) ?? null : null;
  const results = searchCrawlersToRun(rows, query);
  const selectHref = (siteId: string): string => {
    const next = new URLSearchParams();
    if (query) next.set("q", query);
    next.set("site", siteId);
    return `/?${next}#run-heading`;
  };

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">RUN CRAWLER</p>
          <h1>수집할 사이트를,<br />여기서 바로 실행합니다.</h1>
          <p className="lede">사이트를 검색해 선택하면 아래에서 곧바로 수집을 시작할 수 있습니다. 처음 다루는 사이트는 3건 상태 확인으로 점검한 뒤 범위를 넓히는 것을 권장합니다.</p>
        </div>
        <div className="audit-date"><span>CRAWLERS</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>정기 실행이 필요하면 <Link href="/schedules">수집 예약</Link></small></div>
      </header>

      <section className="catalogue-section" aria-labelledby="pick-heading">
        <div className="section-heading">
          <div><p className="eyebrow">SELECT SITE</p><h2 id="pick-heading">수집할 사이트 찾기</h2></div>
          <p>{query ? `${results.total.toLocaleString("ko-KR")}개 일치` : `등록된 수집기 ${rows.length.toLocaleString("ko-KR")}개`}</p>
        </div>
        <Form className="filters filters--run" action="/">
          <label className="query-field"><span>사이트 검색</span><input defaultValue={query} name="q" placeholder="사이트 이름 또는 site_id" /></label>
          <button type="submit">찾기</button>
          {(query || selected) && <Link className="reset-link" href="/">초기화</Link>}
        </Form>

        {query ? (results.matches.length ? <div className="table-shell">
          <table>
            <thead><tr><th>사이트</th><th>수집기 ID</th><th>국가</th><th className="number">수집한 문서</th><th>마지막 검증</th><th>실행</th></tr></thead>
            <tbody>{results.matches.map((row) => {
              const isSelected = selected?.siteId === row.siteId;
              const documents = documentsBySite.get(row.siteId);
              return <tr className={isSelected ? "row-selected" : ""} key={row.siteId}>
                <td className="site-name">{row.siteName || row.siteId}</td>
                <td><code>{row.siteId}</code></td>
                <td>{row.country}</td>
                <td className="number">{documents === undefined ? "-" : documents.toLocaleString("ko-KR")}</td>
                <td><span className={`health-badge health-badge--${row.status}`}><i aria-hidden="true" />{row.status === "healthy" ? "정상" : "실패"}</span></td>
                <td>{isSelected
                  ? <span className="success-note">선택됨</span>
                  : <Link className="run-link" href={selectHref(row.siteId)}>선택 →</Link>}</td>
              </tr>;
            })}</tbody>
          </table>
          {results.truncated && <p className="run-note">일치하는 {results.total.toLocaleString("ko-KR")}개 중 {results.matches.length}개만 표시했습니다. 검색어를 더 좁혀 주세요.</p>}
        </div> : <div className="catalogue-prompt"><span aria-hidden="true">↗</span><div><strong>&ldquo;{query}&rdquo;와 일치하는 수집기가 없습니다.</strong><p>사이트 이름 또는 site_id의 일부로 검색해 보세요. 조건별로 훑어보려면 <Link href="/crawlers">크롤러 상태</Link>에서 국가·자료 유형으로 찾을 수 있습니다.</p></div></div>)
        : <div className="catalogue-prompt"><span aria-hidden="true">↗</span><div><strong>수집할 사이트를 검색해 주세요.</strong><p>사이트 이름이나 site_id의 일부를 입력하면 됩니다. 국가·자료 유형·실패 여부로 훑어보려면 <Link href="/crawlers">크롤러 상태</Link>를 이용하세요.</p></div></div>}

        {requestedSite && !selected && <aside className="snapshot-note snapshot-note--unavailable"><strong>알 수 없는 수집기</strong><span><code>{requestedSite}</code>는 등록된 수집기 카탈로그에 없습니다. 실행 패널을 열지 않았습니다.</span></aside>}
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

import Link from "next/link";
import { Fragment } from "react";
import { AUDIT_DATE, getCrawlerHealth, type CrawlerHealth } from "@/lib/crawler-health";
import { getDatabaseStats, getFreshnessStats } from "@/lib/database-stats";
import { hasActiveCatalogueFilter } from "@/lib/catalogue-filters";
import { JobDashboard } from "./job-dashboard";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 40;
type Params = Promise<Record<string, string | string[] | undefined>>;
const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";

const pageHref = (filters: Readonly<{ q: string; status: string; category: string; country: string; docType: string; groupBy: string }>, page: number): string => {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.status) params.set("status", filters.status);
  if (filters.category) params.set("category", filters.category);
  if (filters.country) params.set("country", filters.country);
  if (filters.docType) params.set("docType", filters.docType);
  if (filters.groupBy) params.set("groupBy", filters.groupBy);
  if (page > 1) params.set("page", String(page));
  return params.size ? `/?${params}` : "/";
};

const HealthBadge = ({ row }: Readonly<{ row: CrawlerHealth }>) => (
  <span className={`health-badge health-badge--${row.status}`}>
    <i aria-hidden="true" />{row.status === "healthy" ? "정상" : "실패"}
  </span>
);

export default async function Home({ searchParams }: Readonly<{ searchParams: Params }>) {
  const params = await searchParams;
  const query = one(params.q).trim();
  const normalizedQuery = query.toLocaleLowerCase("ko-KR");
  const status = one(params.status);
  const category = one(params.category);
  const country = one(params.country);
  const docType = one(params.docType);
  const groupBy = ["country", "docType"].includes(one(params.groupBy)) ? one(params.groupBy) : "";
  const requestedPage = Number.parseInt(one(params.page), 10) || 1;
  const rows = getCrawlerHealth();
  const databaseStats = await getDatabaseStats();
  const freshnessStats = await getFreshnessStats();
  const crawlerById = new Map(rows.map((row) => [row.siteId, row]));
  const measuredCountryCounts = [...(databaseStats?.by_site ?? []).reduce((counts, item) => {
    const name = crawlerById.get(item.key)?.country ?? "기타";
    return counts.set(name, (counts.get(name) ?? 0) + item.documents);
  }, new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const measuredTypeCounts = [...(databaseStats?.by_site ?? []).reduce((counts, item) => {
    const name = crawlerById.get(item.key)?.docType ?? "기타";
    return counts.set(name, (counts.get(name) ?? 0) + item.documents);
  }, new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const healthy = rows.filter((row) => row.status === "healthy").length;
  const unhealthy = rows.length - healthy;
  const categories = [...new Set(rows.map((row) => row.category).filter(Boolean))].sort();
  const countries = [...new Set(rows.map((row) => row.country))].sort((a, b) => a.localeCompare(b, "ko"));
  const docTypes = [...new Set(rows.map((row) => row.docType))].sort((a, b) => a.localeCompare(b, "ko"));
  const countryCounts = [...rows.reduce((counts, row) => counts.set(row.country, (counts.get(row.country) ?? 0) + 1), new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const docTypeCounts = [...rows.reduce((counts, row) => counts.set(row.docType, (counts.get(row.docType) ?? 0) + 1), new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const filtered = rows.filter((row) => {
    const haystack = `${row.siteId} ${row.siteName} ${row.reason}`.toLocaleLowerCase("ko-KR");
    return (!normalizedQuery || haystack.includes(normalizedQuery))
      && (!status || row.status === status)
      && (!category || row.category === category)
      && (!country || row.country === country)
      && (!docType || row.docType === docType);
  }).sort((a, b) => {
    const aGroup = groupBy === "country" ? a.country : groupBy === "docType" ? a.docType : "";
    const bGroup = groupBy === "country" ? b.country : groupBy === "docType" ? b.docType : "";
    return aGroup.localeCompare(bGroup, "ko") || a.siteName.localeCompare(b.siteName, "ko");
  });
  const hasCatalogueSelection = hasActiveCatalogueFilter({ query, status, category, country, docType });
  const selectedRows = hasCatalogueSelection ? filtered : [];
  const pageCount = Math.max(1, Math.ceil(selectedRows.length / PAGE_SIZE));
  const page = Math.min(Math.max(requestedPage, 1), pageCount);
  const visible = selectedRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const filters = { q: query, status, category, country, docType, groupBy };
  const groupLabel = (row: CrawlerHealth): string => groupBy === "country" ? row.country : groupBy === "docType" ? row.docType : "";

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">CRAWLER HEALTH INDEX</p>
          <h1>수집기의 상태를,<br />근거와 함께 살핍니다.</h1>
          <p className="lede">804개 수집기의 마지막 실측 결과를 한곳에서 확인합니다. 정상 판정은 실제 문서가 임시 DB에 저장된 경우에만 부여했습니다.</p>
        </div>
        <div className="audit-date"><span>LAST AUDIT</span><strong>{AUDIT_DATE}</strong><small>실시간 검사는 다음 단계에서 연결됩니다.</small></div>
      </header>

      <section className="status-band" aria-labelledby="band-data-heading">
        <div className="band-heading">
          <div className="band-heading-main">
            <span className="band-number" aria-hidden="true">01</span>
            <div><p className="eyebrow">COLLECTED DATA</p><h2 id="band-data-heading">수집한 데이터</h2></div>
          </div>
          <div className="section-actions"><p>{databaseStats ? `측정 ${new Date(databaseStats.measured_at).toLocaleString("ko-KR")}` : "현재 측정 불가"}</p><Link href="/documents">문서 탐색 →</Link></div>
        </div>
        {databaseStats ? <>
          <div className="summary-grid">
            <article className="summary-card"><span>전체 문서</span><strong>{databaseStats.overview.documents.toLocaleString("ko-KR")}</strong><small>{databaseStats.overview.sites.toLocaleString("ko-KR")}개 사이트</small></article>
            <article className="summary-card summary-card--secondary"><span>PDF 확보</span><strong>{databaseStats.overview.pdf_downloaded.toLocaleString("ko-KR")}</strong><small>{(databaseStats.overview.pdf_bytes / 1024 ** 4).toFixed(1)} TB 메타데이터 합계</small></article>
            <article className="summary-card summary-card--secondary"><span>텍스트 확보</span><strong>{databaseStats.overview.text_extracted.toLocaleString("ko-KR")}</strong><small>추출 완료 문서</small></article>
            <article className="summary-card summary-card--secondary"><span>최신 수집</span><strong className="summary-date">{databaseStats.overview.latest_collected_at ? new Date(databaseStats.overview.latest_collected_at).toLocaleDateString("ko-KR") : "없음"}</strong><small>DB collected_at 기준</small></article>
          </div>
          <aside className="snapshot-note"><strong>정합성</strong><span>고아 문서 {databaseStats.integrity.orphan_documents.toLocaleString("ko-KR")}건 · PDF 메타데이터 누락 {databaseStats.integrity.missing_pdf_metadata.toLocaleString("ko-KR")}건 · blob 파일 전수 검사는 별도 manifest 기준</span></aside>
          <div className="taxonomy-grid" aria-label="실제 문서 분포">
            <article className="taxonomy-panel">
              <div className="taxonomy-heading"><div><p className="eyebrow">DOCUMENTS BY COUNTRY</p><h3>국가별 문서</h3></div><span>DB 실측</span></div>
              <div className="taxonomy-bars">{measuredCountryCounts.slice(0, 10).map(([name, count]) => <div className="measure-row" key={name}><span>{name}</span><i><b style={{ width: `${(count / measuredCountryCounts[0]![1]) * 100}%` }} /></i><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
            </article>
            <article className="taxonomy-panel">
              <div className="taxonomy-heading"><div><p className="eyebrow">DOCUMENTS BY MATERIAL</p><h3>자료 유형별 문서</h3></div><span>미분류는 기타</span></div>
              <div className="type-grid">{measuredTypeCounts.map(([name, count]) => <div className="measure-tile" key={name}><span>{name}</span><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
            </article>
          </div>
        </> : <aside className="snapshot-note snapshot-note--unavailable"><strong>실데이터 연결 대기</strong><span>BE 또는 PostgreSQL에 연결할 수 없습니다. 아래 수집기 감사 스냅샷은 계속 확인할 수 있습니다.</span></aside>}

        <div className="database-status">
          <div className="section-heading"><div><p className="eyebrow">FRESHNESS</p><h3>사이트 최신화</h3></div><p>{freshnessStats ? `측정 ${new Date(freshnessStats.measured_at).toLocaleString("ko-KR")}` : "현재 측정 불가"}</p></div>
          {freshnessStats ? <div className="summary-grid">
            <article className="summary-card summary-card--secondary summary-card--good"><span>7일 이내</span><strong>{(freshnessStats.summary.distribution.within_7_days ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></article>
            <article className="summary-card summary-card--secondary"><span>8~30일</span><strong>{(freshnessStats.summary.distribution["8_to_30_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></article>
            <article className="summary-card summary-card--secondary"><span>31~90일</span><strong>{(freshnessStats.summary.distribution["31_to_90_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></article>
            <article className="summary-card summary-card--secondary summary-card--bad"><span>90일 초과·미수집</span><strong>{((freshnessStats.summary.distribution.over_90_days ?? 0) + freshnessStats.summary.never_collected).toLocaleString("ko-KR")}</strong><small>점검 대상</small></article>
          </div> : <aside className="snapshot-note snapshot-note--unavailable"><strong>최신화 측정 대기</strong><span>BE 또는 PostgreSQL 연결 후 사이트별 마지막 수집일을 계산합니다.</span></aside>}
        </div>
      </section>

      <section className="status-band" aria-labelledby="band-crawlers-heading">
        <div className="band-heading">
          <div className="band-heading-main">
            <span className="band-number" aria-hidden="true">02</span>
            <div><p className="eyebrow">CRAWLER FLEET</p><h2 id="band-crawlers-heading">수집기 상태</h2></div>
          </div>
        </div>

        <div className="summary-grid">
          <article className="summary-card"><span>전체 수집기</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>등록 카탈로그</small></article>
          <article className="summary-card summary-card--good"><span>정상 작동</span><strong>{healthy.toLocaleString("ko-KR")}</strong><small>{rows.length ? ((healthy / rows.length) * 100).toFixed(1) : "0.0"}% 저장 성공</small></article>
          <article className="summary-card summary-card--bad"><span>실패·확인 필요</span><strong>{unhealthy.toLocaleString("ko-KR")}</strong><small>코드·차단·네트워크</small></article>
        </div>

        <aside className="snapshot-note"><strong>스냅샷 안내</strong><span>이 화면은 {AUDIT_DATE} 검증 결과입니다. IP 차단 항목은 납품처 네트워크에서 달라질 수 있습니다.</span></aside>

        <div className="taxonomy-grid" aria-label="국가 및 자료 유형 분류">
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><p className="eyebrow">BY COUNTRY</p><h3>국가별 수집기</h3></div><span>{countries.length}개 국가·지역</span></div>
            <div className="taxonomy-bars">{countryCounts.slice(0, 10).map(([name, count]) => <Link href={`/?country=${encodeURIComponent(name)}`} key={name}><span>{name}</span><i><b style={{ width: `${(count / countryCounts[0]![1]) * 100}%` }} /></i><strong>{count}</strong></Link>)}</div>
          </article>
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><p className="eyebrow">BY MATERIAL</p><h3>자료 유형별 수집기</h3></div><span>{docTypes.length}개 유형</span></div>
            <div className="type-grid">{docTypeCounts.map(([name, count]) => <Link href={`/?docType=${encodeURIComponent(name)}`} key={name}><span>{name}</span><strong>{count}</strong></Link>)}</div>
          </article>
        </div>

        <section className="catalogue-section">
          <div className="section-heading"><div><p className="eyebrow">STATUS CATALOGUE</p><h3>{country || docType ? `${[country, docType].filter(Boolean).join(" · ")} 크롤러` : "분류별 크롤러 목록"}</h3></div><p>{hasCatalogueSelection ? `${selectedRows.length.toLocaleString("ko-KR")}개 결과` : "사이트를 검색하거나 조건을 선택하세요"}</p></div>
          <form className="filters filters--taxonomy" action="/">
            <label className="query-field"><span>사이트 검색</span><input defaultValue={query} name="q" placeholder="사이트 이름 또는 site_id" /></label>
            <label><span>상태</span><select defaultValue={status} name="status"><option value="">전체 상태</option><option value="healthy">정상</option><option value="unhealthy">실패</option></select></label>
            <label><span>실패 유형</span><select defaultValue={category} name="category"><option value="">전체 유형</option>{categories.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label><span>국가</span><select defaultValue={country} name="country"><option value="">전체 국가</option>{countries.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label><span>자료 유형</span><select defaultValue={docType} name="docType"><option value="">전체 자료</option>{docTypes.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label><span>목록 보기</span><select defaultValue={groupBy} name="groupBy"><option value="">전체 목록</option><option value="country">국가별 보기</option><option value="docType">카테고리별 보기</option></select></label>
            <button type="submit">찾기</button>
            <Link className="reset-link" href="/">초기화</Link>
          </form>

          {hasCatalogueSelection ? <div className="table-shell">
            <table>
              <thead><tr><th>상태</th><th>사이트</th><th>국가</th><th>자료 유형</th><th>수집기 ID</th><th className="number">과거 수집</th><th>진단</th><th>실행</th></tr></thead>
              <tbody>{visible.map((row, index) => {
                const previous = visible[index - 1];
                const showGroup = groupBy && (!previous || groupLabel(previous) !== groupLabel(row));
                return <Fragment key={row.siteId}>
                {showGroup && <tr className="group-row"><th colSpan={8}>{groupLabel(row)} <span>{selectedRows.filter((item) => groupLabel(item) === groupLabel(row)).length.toLocaleString("ko-KR")}개</span></th></tr>}
                <tr className={row.status === "unhealthy" ? "row-failed" : ""} key={row.siteId}>
                  <td><HealthBadge row={row} /></td>
                  <td className="site-name">{row.siteName || row.siteId}</td>
                  <td>{row.country}</td>
                  <td><span className="material-tag">{row.docType}</span></td>
                  <td><code>{row.siteId}</code></td>
                  <td className="number">{row.collected.toLocaleString("ko-KR")}</td>
                    <td>{row.category ? <><span className="category">{row.category}</span><small className="reason">{row.reason}</small></> : <span className="success-note">문서 저장 성공</span>}</td>
                    <td><Link className="run-link" href={`/crawlers/${encodeURIComponent(row.siteId)}`}>실행 →</Link></td>
                </tr></Fragment>;
              })}</tbody>
            </table>
            {!visible.length && <div className="empty">조건에 맞는 크롤러가 없습니다.</div>}
          </div> : <div className="catalogue-prompt"><span aria-hidden="true">↗</span><div><strong>검색어를 입력하거나 조건을 선택해 주세요.</strong><p>사이트 이름으로 검색하거나 위의 상태·실패 유형·국가·자료 유형 중 하나를 선택하면 해당 크롤러만 목록에 표시됩니다.</p></div></div>}

          {hasCatalogueSelection && <nav className="pagination" aria-label="페이지 이동">
            {page > 1 ? <Link href={pageHref(filters, page - 1)}>← 이전</Link> : <span />}
            <span>{page} / {pageCount}</span>
            {page < pageCount ? <Link href={pageHref(filters, page + 1)}>다음 →</Link> : <span />}
          </nav>}
        </section>
      </section>
      <JobDashboard />
    </div>
  );
}

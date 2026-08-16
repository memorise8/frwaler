import Form from "next/form";
import Link from "next/link";
import { Fragment } from "react";
import { AUDIT_DATE, getCrawlerHealth, type CrawlerHealth } from "@/lib/crawler-health";
import { getFreshnessStats } from "@/lib/database-stats";
import { hasActiveCatalogueFilter } from "@/lib/catalogue-filters";
import { matchesFreshnessFilter } from "@/lib/freshness-filter";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 40;
type Params = Promise<Record<string, string | string[] | undefined>>;
const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";

const pageHref = (filters: Readonly<{ q: string; status: string; category: string; country: string; docType: string; groupBy: string; freshness: string }>, page: number): string => {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.status) params.set("status", filters.status);
  if (filters.category) params.set("category", filters.category);
  if (filters.country) params.set("country", filters.country);
  if (filters.docType) params.set("docType", filters.docType);
  if (filters.groupBy) params.set("groupBy", filters.groupBy);
  if (filters.freshness) params.set("freshness", filters.freshness);
  if (page > 1) params.set("page", String(page));
  return params.size ? `/crawlers?${params}` : "/crawlers";
};

const HealthBadge = ({ row }: Readonly<{ row: CrawlerHealth }>) => (
  <span className={`health-badge health-badge--${row.status}`}>
    <i aria-hidden="true" />{row.status === "healthy" ? "정상" : "실패"}
  </span>
);

export default async function CrawlerFleet({ searchParams }: Readonly<{ searchParams: Params }>) {
  const params = await searchParams;
  const query = one(params.q).trim();
  const normalizedQuery = query.toLocaleLowerCase("ko-KR");
  const status = one(params.status);
  const category = one(params.category);
  const country = one(params.country);
  const docType = one(params.docType);
  const groupBy = ["country", "docType"].includes(one(params.groupBy)) ? one(params.groupBy) : "";
  const freshness = one(params.freshness);
  const requestedPage = Number.parseInt(one(params.page), 10) || 1;
  const rows = getCrawlerHealth();
  const freshnessStats = await getFreshnessStats();
  const freshnessBucketBySite = new Map((freshnessStats?.sites ?? []).map((site) => [site.site_id, site.freshness_bucket]));
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
      && (!docType || row.docType === docType)
      && (!freshness || matchesFreshnessFilter(freshnessBucketBySite.get(row.siteId) ?? "", freshness));
  }).sort((a, b) => {
    const aGroup = groupBy === "country" ? a.country : groupBy === "docType" ? a.docType : "";
    const bGroup = groupBy === "country" ? b.country : groupBy === "docType" ? b.docType : "";
    return aGroup.localeCompare(bGroup, "ko") || a.siteName.localeCompare(b.siteName, "ko");
  });
  const hasCatalogueSelection = hasActiveCatalogueFilter({ query, status, category, country, docType, freshness });
  const selectedRows = hasCatalogueSelection ? filtered : [];
  const pageCount = Math.max(1, Math.ceil(selectedRows.length / PAGE_SIZE));
  const page = Math.min(Math.max(requestedPage, 1), pageCount);
  const visible = selectedRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const filters = { q: query, status, category, country, docType, groupBy, freshness };
  const groupLabel = (row: CrawlerHealth): string => groupBy === "country" ? row.country : groupBy === "docType" ? row.docType : "";

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">CRAWLER HEALTH INDEX</p>
          <h1>수집기의 상태를,<br />근거와 함께 살핍니다.</h1>
          <p className="lede">{rows.length.toLocaleString("ko-KR")}개 수집기의 마지막 실측 결과를 한곳에서 확인합니다. 정상 판정은 실제 문서가 임시 DB에 저장된 경우에만 부여했습니다.</p>
        </div>
        <div className="audit-date"><span>LAST AUDIT</span><strong>{AUDIT_DATE}</strong><small>실시간 검사는 다음 단계에서 연결됩니다.</small></div>
      </header>

      <div className="summary-grid">
        <article className="summary-card"><span>전체 수집기</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>등록 카탈로그</small></article>
        <Link className="summary-card summary-card--good" href="/crawlers?status=healthy"><span>정상 작동</span><strong>{healthy.toLocaleString("ko-KR")}</strong><small>{rows.length ? ((healthy / rows.length) * 100).toFixed(1) : "0.0"}% 저장 성공</small></Link>
        <Link className="summary-card summary-card--bad" href="/crawlers?status=unhealthy"><span>실패·확인 필요</span><strong>{unhealthy.toLocaleString("ko-KR")}</strong><small>코드·차단·네트워크</small></Link>
      </div>

      <aside className="snapshot-note"><strong>스냅샷 안내</strong><span>이 화면은 {AUDIT_DATE} 검증 결과입니다. IP 차단 항목은 납품처 네트워크에서 달라질 수 있습니다. 지금 상태가 필요하면 <Link href="/">수집 화면</Link>에서 3건 상태 확인을 실행하세요.</span></aside>

      <div className="taxonomy-grid" aria-label="국가 및 자료 유형 분류">
        <article className="taxonomy-panel">
          <div className="taxonomy-heading"><div><p className="eyebrow">BY COUNTRY</p><h2>국가별 수집기</h2></div><span>{countries.length}개 국가·지역</span></div>
          <div className="taxonomy-bars">{countryCounts.slice(0, 10).map(([name, count]) => <Link href={`/crawlers?country=${encodeURIComponent(name)}`} key={name}><span>{name}</span><i><b style={{ width: `${(count / countryCounts[0]![1]) * 100}%` }} /></i><strong>{count}</strong></Link>)}</div>
        </article>
        <article className="taxonomy-panel">
          <div className="taxonomy-heading"><div><p className="eyebrow">BY MATERIAL</p><h2>자료 유형별 수집기</h2></div><span>{docTypes.length}개 유형</span></div>
          <div className="type-grid">{docTypeCounts.map(([name, count]) => <Link href={`/crawlers?docType=${encodeURIComponent(name)}`} key={name}><span>{name}</span><strong>{count}</strong></Link>)}</div>
        </article>
      </div>

      <section className="catalogue-section">
        <div className="section-heading"><div><p className="eyebrow">STATUS CATALOGUE</p><h2>{country || docType ? `${[country, docType].filter(Boolean).join(" · ")} 크롤러` : "분류별 크롤러 목록"}</h2></div><p>{hasCatalogueSelection ? `${selectedRows.length.toLocaleString("ko-KR")}개 결과` : "사이트를 검색하거나 조건을 선택하세요"}</p></div>
        <Form className="filters filters--taxonomy" action="/crawlers">
          <label className="query-field"><span>사이트 검색</span><input defaultValue={query} name="q" placeholder="사이트 이름 또는 site_id" /></label>
          <label><span>상태</span><select defaultValue={status} name="status"><option value="">전체 상태</option><option value="healthy">정상</option><option value="unhealthy">실패</option></select></label>
          <label><span>실패 유형</span><select defaultValue={category} name="category"><option value="">전체 유형</option>{categories.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>국가</span><select defaultValue={country} name="country"><option value="">전체 국가</option>{countries.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>자료 유형</span><select defaultValue={docType} name="docType"><option value="">전체 자료</option>{docTypes.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>목록 보기</span><select defaultValue={groupBy} name="groupBy"><option value="">전체 목록</option><option value="country">국가별 보기</option><option value="docType">카테고리별 보기</option></select></label>
          <label><span>최신화</span><select defaultValue={freshness} name="freshness"><option value="">전체 최신화</option><option value="within_7_days">7일 이내</option><option value="8_to_30_days">8~30일</option><option value="31_to_90_days">31~90일</option><option value="over_90_or_never">90일 초과·미수집</option></select></label>
          <button type="submit">찾기</button>
          <Link className="reset-link" href="/crawlers">초기화</Link>
        </Form>

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
                  <td><Link className="run-link" href={`/crawlers/${encodeURIComponent(row.siteId)}`}>상세 →</Link></td>
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
    </div>
  );
}

import Link from "next/link";
import { getDocumentCatalogue } from "@/lib/document-catalogue";
import { facetOptionsWithSelection, isRetainedFacetOption } from "@/lib/facet-options";

export const dynamic = "force-dynamic";

type Params = Promise<Record<string, string | string[] | undefined>>;
const one = (value: string | string[] | undefined): string => Array.isArray(value) ? value[0] ?? "" : value ?? "";
const ALLOWED = ["q", "site_id", "country", "doc_type", "lang", "published_from", "published_to", "collected_from", "collected_to", "has_pdf", "has_text", "sort", "page"] as const;

const toApiParams = (source: Record<string, string | string[] | undefined>): URLSearchParams => {
  const target = new URLSearchParams();
  for (const key of ALLOWED) {
    const value = one(source[key]).trim();
    if (value) target.set(key, value);
  }
  target.set("page_size", "20");
  return target;
};

const pageHref = (params: URLSearchParams, page: number): string => {
  const next = new URLSearchParams(params);
  next.delete("page_size");
  if (page > 1) next.set("page", String(page)); else next.delete("page");
  return `/documents${next.size ? `?${next}` : ""}`;
};

const readableDate = (value: string | null): string => {
  if (!value) return "날짜 미상";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString("ko-KR");
};

export default async function DocumentsPage({ searchParams }: Readonly<{ searchParams: Params }>) {
  const source = await searchParams;
  const apiParams = toApiParams(source);
  const result = await getDocumentCatalogue(apiParams);
  const selected = Object.fromEntries(ALLOWED.map((key) => [key, one(source[key]).trim()]));
  const data = result.ok ? result.data : null;

  return <div className="documents-page">
    <header className="documents-hero">
      <div><p className="eyebrow">LIBERTREE ARCHIVE</p><h1>수집된 지식을<br />한곳에서 탐색합니다.</h1></div>
      <p>정책 보고서, 연구자료, 논문과 공공 기록을 제목과 본문으로 검색하고 원문 확보 상태까지 확인할 수 있습니다.</p>
    </header>

    <section className="document-search" aria-labelledby="document-search-title">
      <div className="section-heading"><div><p className="eyebrow">DOCUMENT SEARCH</p><h2 id="document-search-title">문서 검색</h2></div><p>{data ? `${data.pagination.total.toLocaleString("ko-KR")}건` : "조회 대기"}</p></div>
      <form action="/documents" className="document-filters">
        <label className="document-query"><span>통합 검색</span><input name="q" defaultValue={selected.q} placeholder="제목, 초록, 키워드 검색" autoFocus /></label>
        <label><span>국가</span><select name="country" defaultValue={selected.country}><option value="">전체 국가</option>{facetOptionsWithSelection(data?.facets.countries, selected.country).map((item) => <option key={item.value} value={item.value}>{item.value} ({isRetainedFacetOption(item) ? "선택값 유지" : item.count.toLocaleString("ko-KR")})</option>)}</select></label>
        <label><span>자료 유형</span><select name="doc_type" defaultValue={selected.doc_type}><option value="">전체 자료</option>{facetOptionsWithSelection(data?.facets.doc_types, selected.doc_type).map((item) => <option key={item.value} value={item.value}>{item.value} ({isRetainedFacetOption(item) ? "선택값 유지" : item.count.toLocaleString("ko-KR")})</option>)}</select></label>
        <label><span>사이트</span><select name="site_id" defaultValue={selected.site_id}><option value="">전체 사이트</option>{facetOptionsWithSelection(data?.facets.sites, selected.site_id).map((item) => <option key={item.value} value={item.value}>{item.label || item.value} ({isRetainedFacetOption(item) ? "선택값 유지" : item.count.toLocaleString("ko-KR")})</option>)}</select></label>
        <label><span>언어</span><select name="lang" defaultValue={selected.lang}><option value="">전체 언어</option>{facetOptionsWithSelection(data?.facets.languages, selected.lang).map((item) => <option key={item.value} value={item.value}>{item.value} ({isRetainedFacetOption(item) ? "선택값 유지" : item.count.toLocaleString("ko-KR")})</option>)}</select></label>
        <label><span>발행 시작일</span><input type="date" name="published_from" defaultValue={selected.published_from} /></label>
        <label><span>발행 종료일</span><input type="date" name="published_to" defaultValue={selected.published_to} /></label>
        <label><span>수집 시작일</span><input type="date" name="collected_from" defaultValue={selected.collected_from} /></label>
        <label><span>수집 종료일</span><input type="date" name="collected_to" defaultValue={selected.collected_to} /></label>
        <label><span>PDF</span><select name="has_pdf" defaultValue={selected.has_pdf}><option value="">전체</option><option value="true">확보</option><option value="false">미확보</option></select></label>
        <label><span>텍스트</span><select name="has_text" defaultValue={selected.has_text}><option value="">전체</option><option value="true">확보</option><option value="false">미확보</option></select></label>
        <label><span>정렬</span><select name="sort" defaultValue={selected.sort}><option value="">기본 정렬</option>{selected.q && <option value="relevance">관련도순</option>}<option value="published_desc">최신 발행일</option><option value="collected_desc">최신 수집일</option><option value="seq_desc">최근 등록순</option></select></label>
        <div className="document-filter-actions"><button type="submit">검색</button><Link href="/documents">조건 초기화</Link></div>
      </form>
    </section>

    {!result.ok && <section className={`catalogue-message catalogue-message--${result.kind}`} role="status"><strong>{result.kind === "invalid" ? "검색 조건을 확인해 주세요." : "문서 목록을 불러올 수 없습니다."}</strong><p>{result.kind === "invalid" ? "날짜 범위나 필터 값이 올바른지 확인한 뒤 다시 검색해 주세요." : "데이터 서비스 연결 상태를 확인한 뒤 잠시 후 다시 시도해 주세요."}</p></section>}

    {data && <section className="document-results" aria-label="문서 검색 결과">
      <div className="result-meta"><span>총 <strong>{data.pagination.total.toLocaleString("ko-KR")}</strong>건</span><span>{data.pagination.pages ? `${data.pagination.page} / ${data.pagination.pages} 페이지` : "결과 없음"}</span></div>
      {data.items.length ? <ol className="document-list">{data.items.map((item) => <li key={item.seq_id}>
        <article className="document-card">
          <div className="document-card-index">{String(item.seq_id).padStart(6, "0")}</div>
          <div className="document-card-main">
            <div className="document-kicker"><span>{item.country}</span><span>{item.doc_type}</span><span>{item.site_name}</span></div>
            <h2><Link href={`/documents/${item.seq_id}`}>{item.title}</Link></h2>
            <p className="document-byline">{[item.authors, item.publisher, item.journal].filter(Boolean).join(" · ") || "저자·발행처 정보 없음"}</p>
            <div className="document-badges"><span>{item.lang === "unknown" ? "언어 미측정" : item.lang.toUpperCase()}</span>{item.has_pdf && <span className="available">PDF</span>}{item.has_text && <span className="available">TEXT</span>}{item.has_translation && <span className="translated">한국어 번역</span>}</div>
          </div>
          <div className="document-card-side"><span>발행 {readableDate(item.published_date)}</span><span>수집 {readableDate(item.collected_at)}</span><a href={item.meta_url} target="_blank" rel="noreferrer">원문 사이트 ↗</a></div>
        </article>
      </li>)}</ol> : <div className="document-empty"><strong>조건에 맞는 문서가 없습니다.</strong><p>검색어를 줄이거나 필터 조건을 초기화해 보세요.</p></div>}
      {data.pagination.pages > 1 && <nav className="pagination" aria-label="문서 결과 페이지 이동">
        {data.pagination.page > 1 ? <Link href={pageHref(apiParams, data.pagination.page - 1)}>← 이전</Link> : <span />}
        <span>{data.pagination.page} / {data.pagination.pages}</span>
        {data.pagination.page < data.pagination.pages ? <Link href={pageHref(apiParams, data.pagination.page + 1)}>다음 →</Link> : <span />}
      </nav>}
    </section>}
  </div>;
}

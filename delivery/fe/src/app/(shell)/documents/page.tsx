import { documentDate } from "@/lib/document-date";
import Form from "next/form";
import Link from "next/link";
import { getDocumentCatalogue } from "@/lib/document-catalogue";
import { facetCountLabel, facetOptionsWithSelection } from "@/lib/facet-options";
import { RELEVANCE_SORT_LABEL, buildCatalogueQuery, cursorHref, defaultSortLabel, formatTotal, hasAdvancedFilters, invalidCatalogueMessage } from "@/lib/document-paging";
import { formatSeoulDate } from "@/lib/seoul-time";
import SearchFields from "./search-fields";
import styles from "./documents.module.css";

export const dynamic = "force-dynamic";

type Params = Promise<Record<string, string | string[] | undefined>>;

export default async function DocumentsPage({ searchParams }: Readonly<{ searchParams: Params }>) {
  const source = await searchParams;
  const { apiParams, selected, qNotice } = buildCatalogueQuery(source);
  const result = await getDocumentCatalogue(apiParams);
  const data = result.ok ? result.data : null;
  const totalLabel = data ? formatTotal(data.pagination.total, data.pagination.total_exact, data.total?.source) : null;
  const prevCursor = data?.pagination.prev_cursor ?? null;
  const nextCursor = data?.pagination.next_cursor ?? null;
  const onFirstPage = !selected.cursor && !apiParams.has("page");

  return <div className={`documents-page ${styles.page}`}>
    <header className={styles.header}>
      <h1>문서 탐색</h1>
      <p>{totalLabel ? `전체 ${totalLabel}` : "조회 대기"} · 제목·초록 검색은 사이트를 고른 뒤 쓸 수 있습니다.</p>
    </header>

    <section aria-label="문서 검색">
      <Form action="/documents" className={`document-filters ${styles.form}`}>
        <div className={styles.mainRow}>
          <SearchFields sites={facetOptionsWithSelection(data?.facets.sites, selected.site_id)} siteId={selected.site_id} q={selected.q} />
          <div className={`document-filter-actions ${styles.actions}`}><button type="submit">검색</button><Link href="/documents">조건 초기화</Link></div>
        </div>
        <details className={styles.more} open={hasAdvancedFilters(selected)}>
          <summary>상세 조건 (국가·자료 유형·언어·날짜·파일·정렬)</summary>
          <div className={styles.moreGrid}>
            <label><span>국가</span><select name="country" defaultValue={selected.country}><option value="">전체 국가</option>{facetOptionsWithSelection(data?.facets.countries, selected.country).map((item) => <option key={item.value} value={item.value}>{item.value}{facetCountLabel(item)}</option>)}</select></label>
            <label><span>자료 유형</span><select name="doc_type" defaultValue={selected.doc_type}><option value="">전체 자료</option>{facetOptionsWithSelection(data?.facets.doc_types, selected.doc_type).map((item) => <option key={item.value} value={item.value}>{item.value}{facetCountLabel(item)}</option>)}</select></label>
            <label><span>언어</span><select name="lang" defaultValue={selected.lang}><option value="">전체 언어</option>{facetOptionsWithSelection(data?.facets.languages, selected.lang).map((item) => <option key={item.value} value={item.value}>{item.value}{facetCountLabel(item)}</option>)}</select></label>
            <label><span>정렬</span><select name="sort" defaultValue={selected.sort}><option value="">{defaultSortLabel(selected.site_id ? selected.q : "")}</option>{selected.q && selected.site_id && <option value="relevance">{RELEVANCE_SORT_LABEL}</option>}<option value="published_desc">최신 발행일</option><option value="collected_desc">최신 수집일</option><option value="seq_desc">최근 등록순</option></select></label>
            <label><span>발행 시작일</span><input type="date" name="published_from" defaultValue={selected.published_from} /></label>
            <label><span>발행 종료일</span><input type="date" name="published_to" defaultValue={selected.published_to} /></label>
            <label><span>수집 시작일</span><input type="date" name="collected_from" defaultValue={selected.collected_from} /></label>
            <label><span>수집 종료일</span><input type="date" name="collected_to" defaultValue={selected.collected_to} /></label>
            <label><span>PDF</span><select name="has_pdf" defaultValue={selected.has_pdf}><option value="">전체</option><option value="true">확보</option><option value="false">미확보</option></select></label>
            <label><span>텍스트</span><select name="has_text" defaultValue={selected.has_text}><option value="">전체</option><option value="true">확보</option><option value="false">미확보</option></select></label>
            <p className={styles.note}>발행일 범위 검색에는 연·월·일이 확인된 문서만 포함됩니다. 부분 날짜나 확인이 필요한 날짜의 문서는 날짜 조건을 해제해 확인하세요.</p>
          </div>
        </details>
      </Form>
      {qNotice && <p className="catalogue-message catalogue-message--invalid" role="status">{qNotice}</p>}
    </section>

    {!result.ok && <section className={`catalogue-message catalogue-message--${result.kind}`} role="status"><strong>{result.kind === "invalid" ? "검색 조건을 확인해 주세요." : "문서 목록을 불러올 수 없습니다."}</strong><p>{result.kind === "invalid" ? invalidCatalogueMessage(result.reason) : "데이터 서비스 연결 상태를 확인한 뒤 잠시 후 다시 시도해 주세요."}</p>{result.kind === "invalid" && selected.cursor && <Link href={cursorHref(apiParams, null)}>첫 페이지로 →</Link>}</section>}

    {data && <section className="document-results" aria-label="문서 검색 결과">
      <div className="result-meta"><span>총 <strong>{totalLabel}</strong></span><span>{data.items.length ? (onFirstPage ? "첫 페이지" : "이어서 보기") : "결과 없음"}</span></div>
      {data.items.length ? <ol className="document-list">{data.items.map((item) => <li key={item.seq_id}>
        <article className="document-card">
          <div className="document-card-index">{String(item.seq_id).padStart(6, "0")}</div>
          <div className="document-card-main">
            <div className="document-kicker"><span>{item.country}</span><span>{item.doc_type}</span><span>{item.site_name}</span></div>
            <h3><Link href={`/documents/${item.seq_id}`}>{item.title}</Link></h3>
            <p className="document-byline">{[item.authors, item.publisher, item.journal].filter(Boolean).join(" · ") || "저자·발행처 정보 없음"}</p>
            <div className="document-badges"><span>{item.lang === "unknown" ? "언어 미측정" : item.lang.toUpperCase()}</span>{item.has_pdf && <span className="available">PDF</span>}{item.has_text && <span className="available">TEXT</span>}{item.has_translation && <span className="translated">한국어 번역</span>}</div>
          </div>
          <div className="document-card-side"><span>발행 {documentDate(item.published_date, item.published_date_raw, item.published_date_status)}</span><span>수집 {formatSeoulDate(item.collected_at, "날짜 미상")}</span><a href={item.meta_url} target="_blank" rel="noreferrer">원문 사이트 ↗</a></div>
        </article>
      </li>)}</ol> : <div className="document-empty"><strong>조건에 맞는 문서가 없습니다.</strong><p>검색어를 줄이거나 필터 조건을 초기화해 보세요.</p></div>}
      {(prevCursor || nextCursor || !onFirstPage) && <nav className="pagination" aria-label="문서 결과 페이지 이동">
        {prevCursor ? <Link href={cursorHref(apiParams, prevCursor)} rel="prev">← 이전</Link> : !onFirstPage ? <Link href={cursorHref(apiParams, null)}>← 처음</Link> : <span />}
        <span>{onFirstPage ? "첫 페이지" : `${data.pagination.page_size}건씩`}</span>
        {nextCursor ? <Link href={cursorHref(apiParams, nextCursor)} rel="next">다음 →</Link> : <span />}
      </nav>}
    </section>}
  </div>;
}

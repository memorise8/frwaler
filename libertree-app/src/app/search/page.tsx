import Link from "next/link"
import { getCatalogueBrowseSummary, getCatalogueScopedCountries, getCatalogueSiteSources, searchCatalogue } from "../../lib/catalogue"
import type { CatalogueBrowseBucket } from "../../lib/catalogue"

export const dynamic = "force-dynamic"
export const metadata = { title: "자료 탐색" }

type SearchParams = Readonly<Record<string, string | readonly string[] | undefined>>
const SITE_FACET_LIMIT = 48
const stringValue = (value: string | readonly string[] | undefined): string | undefined => typeof value === "string" && value.length > 0 ? value : undefined
const pageValue = (value: string | undefined): number | undefined => value !== undefined && /^\d{1,5}$/.test(value) ? Number(value) : undefined
const query = (params: SearchParams, name: string): string | undefined => stringValue(params[name])

export default async function SearchPage({ searchParams }: Readonly<{ searchParams: Promise<SearchParams> }>): Promise<React.JSX.Element> {
  const params = await searchParams
  const q = query(params, "q")
  const browse = getCatalogueBrowseSummary()
  const category = pick(query(params, "category"), browse.categories)
  const continent = pick(query(params, "continent"), browse.continents)
  const country = pick(query(params, "country"), browse.countries)
  const docType = pick(query(params, "docType"), browse.docTypes)
  const scope = { category, continent, country, docType }
  // Progressive drill-down: 유형/대륙/기관성격 → 나라 → 사이트.
  const hasNarrowingScope = category !== undefined || continent !== undefined || docType !== undefined
  const showCountryFacet = country === undefined && hasNarrowingScope
  const countryBuckets = showCountryFacet ? getCatalogueScopedCountries(scope) : []
  const siteSources = country !== undefined ? getCatalogueSiteSources(scope) : []
  const siteParam = query(params, "site")
  const site = siteParam !== undefined && siteSources.some((bucket) => bucket.key === siteParam) ? siteParam : undefined
  const result = searchCatalogue({ q, ...scope, siteId: site, page: pageValue(query(params, "page")) })
  const pages = Math.max(1, Math.ceil(result.total / 20))
  const scopeHref = (overrides: Readonly<Record<string, string | undefined>>): string => {
    const values = new URLSearchParams()
    for (const [name, value] of [["q", q], ["category", category], ["continent", continent], ["country", country], ["docType", docType], ["site", site], ...Object.entries(overrides)] as const) {
      if (value !== undefined) values.set(name, value)
      else values.delete(name)
    }
    return `/search?${values.toString()}`
  }
  return (
    <section className="search-page stack">
      <div><p className="page-label">CATALOGUE / EXPLORE</p><h1>자료 탐색</h1><p className="lede">제목, 주제, 출처를 기준으로 자료를 찾거나 서가의 분류를 조합해 보세요.</p></div>
      <form className="search-frame" aria-label="자료 검색">
        <label htmlFor="catalogue-query">무엇을 찾고 계신가요?</label>
        <div className="search-frame__controls"><input id="catalogue-query" name="q" defaultValue={q ?? ""} placeholder="제목 · 주제 · 출처" /><button type="submit">검색</button></div>
        <div className="filter-row">
          <label>유형<select name="docType" defaultValue={docType ?? ""}><option value="">전체</option>{browse.docTypes.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label>
          <label>대륙<select name="continent" defaultValue={continent ?? ""}><option value="">전체</option>{browse.continents.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label>
          <label>국가·지역<select name="country" defaultValue={country ?? ""}><option value="">전체</option>{browse.countries.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label>
          <label>기관 성격<select name="category" defaultValue={category ?? ""}><option value="">전체</option>{browse.categories.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label>
        </div>
        <Link className="filter-reset" href="/search">분류와 검색어 초기화</Link>
      </form>
      {showCountryFacet && countryBuckets.length > 0 ? <CountryFacet buckets={countryBuckets} scopeHref={scopeHref} /> : null}
      {country !== undefined && siteSources.length > 0 ? <SiteFacet buckets={siteSources} activeSite={site} scopeHref={scopeHref} backHref={hasNarrowingScope ? scopeHref({ country: undefined, site: undefined, page: undefined }) : undefined} /> : null}
      <p className="lede">결과 {result.total.toLocaleString("ko-KR")}건 · {result.page} / {pages} 페이지</p>
      <ul className="result-list">{result.items.map((item) => <li key={item.id}><Link href={`/search/${item.id}`}><p className="page-label">{item.source.docType} · {item.source.country} · {item.source.category}</p><h2>{item.title}</h2><p className="lede">{item.publishedDate === null ? "발행일 정보 없음" : `발행일 ${item.publishedDate}`}</p><p>{item.description.text ?? "소개 정보가 아직 없습니다."}</p></Link></li>)}</ul>
      {result.items.length === 0 ? <section className="empty-state"><h2>검색 결과가 없습니다.</h2><p>다른 검색어나 분류를 사용해 보세요.</p></section> : null}
      <nav className="pagination" aria-label="검색 결과 페이지"><PageLink href={scopeHref({ page: String(result.page - 1) })} disabled={result.page <= 1}>이전</PageLink><PageLink href={scopeHref({ page: String(result.page + 1) })} disabled={result.page >= pages}>다음</PageLink></nav>
    </section>
  )
}

const pick = (value: string | undefined, buckets: readonly CatalogueBrowseBucket[]): string | undefined => value !== undefined && buckets.some((bucket) => bucket.key === value) ? value : undefined

const CountryFacet = ({ buckets, scopeHref }: Readonly<{ buckets: readonly CatalogueBrowseBucket[]; scopeHref: (overrides: Readonly<Record<string, string | undefined>>) => string }>): React.JSX.Element => (
  <section className="browse-shelf" aria-labelledby="browse-country">
    <div className="browse-shelf__heading">
      <div><p className="page-label">BROWSE</p><h2 id="browse-country">국가·지역으로 좁히기</h2></div>
      <p className="lede">이 분류의 자료가 있는 국가·지역입니다. 국가를 선택하면 해당 국가의 수집 출처(사이트)로 이어집니다.</p>
    </div>
    <ul className="browse-grid browse-grid--dense">
      {buckets.map((bucket) => <li key={bucket.key}>
        <Link className="browse-card" href={scopeHref({ country: bucket.key, site: undefined, page: undefined })}>
          <span className="browse-card__label">{bucket.label}</span>
          <span className="browse-card__count">{bucket.count.toLocaleString("ko-KR")}건</span>
          <span className="browse-card__action">출처 보기</span>
        </Link>
      </li>)}
    </ul>
  </section>
)

const SiteFacet = ({ activeSite, backHref, buckets, scopeHref }: Readonly<{ activeSite: string | undefined; backHref: string | undefined; buckets: readonly CatalogueBrowseBucket[]; scopeHref: (overrides: Readonly<Record<string, string | undefined>>) => string }>): React.JSX.Element => {
  const shown = buckets.slice(0, SITE_FACET_LIMIT)
  const remainder = buckets.length - shown.length
  return (
    <section className="browse-shelf" aria-labelledby="browse-site">
      <div className="browse-shelf__heading">
        <div><p className="page-label">BROWSE</p><h2 id="browse-site">수집 출처(사이트)로 좁히기</h2></div>
        <p className="lede">{activeSite === undefined ? "이 분류의 자료를 수집한 출처입니다. 특정 사이트를 선택해 좁혀 보세요." : <>특정 출처의 자료만 보고 있습니다. <Link href={scopeHref({ site: undefined })}>모든 출처 보기</Link></>}{backHref !== undefined ? <> · <Link href={backHref}>다른 국가 선택</Link></> : null}</p>
      </div>
      <ul className="browse-grid browse-grid--dense">
        {shown.map((bucket) => <li key={bucket.key}>
          <Link className="browse-card" href={scopeHref({ site: bucket.key, page: undefined })} aria-current={bucket.key === activeSite ? "true" : undefined}>
            <span className="browse-card__label">{bucket.label}</span>
            <span className="browse-card__count">{bucket.count.toLocaleString("ko-KR")}건</span>
            <span className="browse-card__action">{bucket.key === activeSite ? "선택됨" : "자료 보기"}</span>
          </Link>
        </li>)}
      </ul>
      {remainder > 0 ? <p className="lede">문서 수 기준 상위 {SITE_FACET_LIMIT}개 출처만 표시했습니다. (외 {remainder.toLocaleString("ko-KR")}개 · 국가·유형 등으로 범위를 좁히면 더 정확합니다.)</p> : null}
    </section>
  )
}

const PageLink = ({ children, disabled, href }: Readonly<{ children: string; disabled: boolean; href: string }>): React.JSX.Element =>
  disabled ? <span aria-disabled="true">{children}</span> : <Link href={href}>{children}</Link>

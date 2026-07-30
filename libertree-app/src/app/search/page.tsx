import Link from "next/link"
import { getCatalogueBrowseSummary, searchCatalogue } from "../../lib/catalogue"

export const dynamic = "force-dynamic"
export const metadata = { title: "자료 탐색" }

type SearchParams = Readonly<Record<string, string | readonly string[] | undefined>>
const stringValue = (value: string | readonly string[] | undefined): string | undefined => typeof value === "string" && value.length > 0 ? value : undefined
const pageValue = (value: string | undefined): number | undefined => value !== undefined && /^\d{1,5}$/.test(value) ? Number(value) : undefined
const query = (params: SearchParams, name: string): string | undefined => stringValue(params[name])

export default async function SearchPage({ searchParams }: Readonly<{ searchParams: Promise<SearchParams> }>): Promise<React.JSX.Element> {
  const params = await searchParams
  const category = query(params, "category")
  const continent = query(params, "continent")
  const country = query(params, "country")
  const browse = getCatalogueBrowseSummary()
  const hasCategory = browse.categories.some((bucket) => bucket.key === category)
  const hasContinent = browse.continents.some((bucket) => bucket.key === continent)
  const hasCountry = browse.countries.some((bucket) => bucket.key === country)
  const result = searchCatalogue({ q: query(params, "q"), category: hasCategory ? category : undefined, continent: hasContinent ? continent : undefined, country: hasCountry ? country : undefined, page: pageValue(query(params, "page")) })
  const pages = Math.max(1, Math.ceil(result.total / 20))
  return <section className="search-page stack"><div><p className="page-label">CATALOGUE / EXPLORE</p><h1>자료 탐색</h1><p className="lede">제목, 주제, 출처를 기준으로 자료를 찾거나 서가의 분류를 조합해 보세요.</p></div><form className="search-frame" aria-label="자료 검색"><label htmlFor="catalogue-query">무엇을 찾고 계신가요?</label><div className="search-frame__controls"><input id="catalogue-query" name="q" defaultValue={query(params, "q") ?? ""} placeholder="제목 · 주제 · 출처"/><button type="submit">검색</button></div><div className="filter-row"><label>대륙<select name="continent" defaultValue={hasContinent ? continent : ""}><option value="">전체</option>{browse.continents.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label><label>국가·지역<select name="country" defaultValue={hasCountry ? country : ""}><option value="">전체</option>{browse.countries.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label><label>기관 성격<select name="category" defaultValue={hasCategory ? category : ""}><option value="">전체</option>{browse.categories.map((bucket) => <option key={bucket.key} value={bucket.key}>{bucket.label}</option>)}</select></label></div><Link className="filter-reset" href="/search">분류와 검색어 초기화</Link></form><p className="lede">결과 {result.total.toLocaleString("ko-KR")}건 · {result.page} / {pages} 페이지</p><ul className="result-list">{result.items.map((item) => <li key={item.id}><Link href={`/search/${item.id}`}><p className="page-label">{item.source.country} · {item.source.category}</p><h2>{item.title}</h2><p className="lede">{item.publishedDate === null ? "발행일 정보 없음" : `발행일 ${item.publishedDate}`}</p><p>{item.description.text ?? "소개 정보가 아직 없습니다."}</p></Link></li>)}</ul>{result.items.length === 0 ? <section className="empty-state"><h2>검색 결과가 없습니다.</h2><p>다른 검색어나 분류를 사용해 보세요.</p></section> : null}<nav className="pagination" aria-label="검색 결과 페이지"><PageLink page={result.page - 1} disabled={result.page <= 1} params={params}>이전</PageLink><PageLink page={result.page + 1} disabled={result.page >= pages} params={params}>다음</PageLink></nav></section>
}

const PageLink = ({ children, disabled, page, params }: Readonly<{ children: string; disabled: boolean; page: number; params: SearchParams }>): React.JSX.Element => {
  if (disabled) return <span aria-disabled="true">{children}</span>
  const values = new URLSearchParams()
  for (const name of ["q", "category", "continent", "country"] as const) { const value = query(params, name); if (value !== undefined) values.set(name, value) }
  values.set("page", String(page))
  return <Link href={`/search?${values.toString()}`}>{children}</Link>
}

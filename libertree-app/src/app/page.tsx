import Link from "next/link"
import { getCatalogueBrowseSummary } from "../lib/catalogue"

export const dynamic = "force-dynamic"

const browseHref = (name: "category" | "continent" | "country", value: string): string => `/search?${new URLSearchParams([[name, value]]).toString()}`

export default function HomePage(): React.JSX.Element {
  const browse = getCatalogueBrowseSummary()
  return (
    <section className="home stack">
      <p className="page-label">GLOBAL DOCUMENT CATALOGUE</p>
      <div className="home__heading stack">
        <h1>세계의 자료를,<br />출처를 따라 읽는 서가.</h1>
        <p className="lede">
          Libertree는 전 세계 공공·연구·학술 자료를 출처별로 보존하고 탐색하는 읽기 전용 서가입니다.
        </p>
      </div>
      <form className="home-search" action="/search" aria-label="자료 검색">
        <label htmlFor="home-catalogue-query">제목, 주제 또는 출처로 찾기</label>
        <div className="search-frame__controls">
          <input id="home-catalogue-query" name="q" placeholder="예: 교육, 기후, 연구기관" />
          <button type="submit">검색</button>
        </div>
      </form>
      <div className="catalogue-overview" aria-label="카탈로그 현황">
        <p><strong>{browse.totalDocuments.toLocaleString("ko-KR")}</strong>건의 자료</p>
        <p><strong>{browse.sourceCount.toLocaleString("ko-KR")}</strong>개 수집 출처</p>
        <Link className="catalogue-link" href="/search">전체 자료 보기</Link>
      </div>
      <BrowseShelf title="대륙으로 둘러보기" lead="자료가 수집된 지역을 선택해 해당 출처의 문서를 살펴보세요." buckets={browse.continents} filter="continent" />
      <BrowseShelf title="국가·지역으로 둘러보기" lead="국가별 수집 자료를 바로 열 수 있습니다." buckets={browse.countries} filter="country" dense />
      <BrowseShelf title="기관 성격으로 둘러보기" lead="정부, 연구기관, 대학 등 자료를 만든 기관의 성격으로 탐색합니다." buckets={browse.categories} filter="category" />
      <aside className="catalogue-note">
        <p className="card-label">READ-ONLY CATALOGUE</p>
        <p>이 서가는 자료를 소개하고 원문으로 연결합니다. 수집·갱신·운영 기능은 이 화면에 포함하지 않습니다.</p>
      </aside>
    </section>
  )
}

const BrowseShelf = ({ buckets, dense = false, filter, lead, title }: Readonly<{ buckets: readonly { readonly count: number; readonly key: string; readonly label: string }[]; dense?: boolean; filter: "category" | "continent" | "country"; lead: string; title: string }>): React.JSX.Element => (
  <section className="browse-shelf" aria-labelledby={`browse-${filter}`}>
    <div className="browse-shelf__heading">
      <div>
        <p className="page-label">BROWSE</p>
        <h2 id={`browse-${filter}`}>{title}</h2>
      </div>
      <p className="lede">{lead}</p>
    </div>
    <ul className={`browse-grid${dense ? " browse-grid--dense" : ""}`}>
      {buckets.map((bucket) => <li key={bucket.key}>
        <Link className="browse-card" href={browseHref(filter, bucket.key)}>
          <span className="browse-card__label">{bucket.label}</span>
          <span className="browse-card__count">{bucket.count.toLocaleString("ko-KR")}건</span>
          <span className="browse-card__action">자료 보기</span>
        </Link>
      </li>)}
    </ul>
  </section>
)

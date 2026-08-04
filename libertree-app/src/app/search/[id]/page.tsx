import Link from "next/link"
import { notFound } from "next/navigation"
import { getCatalogueDocument } from "../../../lib/catalogue"
import { LanguageToggle } from "./language-toggle"

export const dynamic = "force-dynamic"
export const metadata = { title: "문서 상세" }

export default async function DocumentPage({ params }: Readonly<{ params: Promise<{ readonly id: string }> }>): Promise<React.JSX.Element> {
  const { id } = await params
  if (!/^\d{1,12}$/.test(id)) notFound()
  const document = getCatalogueDocument(Number.parseInt(id, 10))
  if (document === null) notFound()
  return <section className="search-page stack"><Link className="catalogue-link" href="/search">자료 탐색으로 돌아가기</Link><article className="document-card stack"><LanguageToggle sourceLabel={`${document.source.country} · ${document.source.category}`} title={document.title} titleKo={document.titleKo} publishedDate={document.publishedDate} description={document.description} descriptionKo={document.descriptionKo} />{document.authors.length || document.publisher || document.journal || document.keywords.length ? <section className="bibliography stack"><h2>서지 정보</h2>{document.authors.length ? <p><strong>저자</strong> {document.authors.join(", ")}</p> : null}{document.publisher ? <p><strong>발행기관</strong> {document.publisher}</p> : null}{document.journal ? <p><strong>저널</strong> {document.journal}</p> : null}{document.keywords.length ? <p><strong>키워드</strong> {document.keywords.join(" · ")}</p> : null}</section> : null}<section>{document.originalSourceUrl === null ? <p className="lede">검증 가능한 원문 출처 링크가 등록되어 있지 않습니다.</p> : <a className="source-cta" href={document.originalSourceUrl} target="_blank" rel="noopener noreferrer">원문 출처에서 보기</a>}</section></article></section>
}

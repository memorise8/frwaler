import Link from "next/link"
import { notFound } from "next/navigation"
import { getCatalogueDocument } from "../../../lib/catalogue"
import type { InternalReviewDescription } from "../../../lib/internal-review"

export const dynamic = "force-dynamic"
export const metadata = { title: "문서 상세" }

export default async function DocumentPage({ params }: Readonly<{ params: Promise<{ readonly id: string }> }>): Promise<React.JSX.Element> {
  const { id } = await params
  if (!/^\d{1,12}$/.test(id)) notFound()
  const document = getCatalogueDocument(Number.parseInt(id, 10))
  if (document === null) notFound()
  return <section className="search-page stack"><Link className="catalogue-link" href="/search">자료 탐색으로 돌아가기</Link><article className="document-card stack"><header className="stack"><p className="page-label">{document.source.country} · {document.source.category}</p><h1>{document.title}</h1>{document.publishedDate === null ? null : <p className="lede">발행일 {document.publishedDate}</p>}</header><Description description={document.description} />{document.authors.length || document.publisher || document.journal || document.keywords.length ? <section className="bibliography stack"><h2>서지 정보</h2>{document.authors.length ? <p><strong>저자</strong> {document.authors.join(", ")}</p> : null}{document.publisher ? <p><strong>발행기관</strong> {document.publisher}</p> : null}{document.journal ? <p><strong>저널</strong> {document.journal}</p> : null}{document.keywords.length ? <p><strong>키워드</strong> {document.keywords.join(" · ")}</p> : null}</section> : null}<section>{document.originalSourceUrl === null ? <p className="lede">검증 가능한 원문 출처 링크가 등록되어 있지 않습니다.</p> : <a className="source-cta" href={document.originalSourceUrl} target="_blank" rel="noopener noreferrer">원문 출처에서 보기</a>}</section></article></section>
}

const Description = ({ description }: Readonly<{ description: InternalReviewDescription }>): React.JSX.Element => {
  const label = description.provenance === "summary" ? "자료 소개 · 요약 기반" : description.provenance === "abstract" ? "자료 소개 · 초록 기반" : "자료 소개"
  return <section className="description-card stack"><h2>{label}</h2>{description.text === null ? <p>요약 또는 초록 정보가 아직 준비되지 않았습니다.</p> : <p>{description.text}</p>}{description.truncated ? <p className="lede">검토 화면에서는 소개문을 일부만 표시합니다.</p> : null}</section>
}

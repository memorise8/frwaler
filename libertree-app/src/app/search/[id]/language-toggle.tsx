"use client"

import { useState } from "react"
import type { InternalReviewDescription } from "../../../lib/internal-review"

type LanguageToggleProps = Readonly<{
  description: InternalReviewDescription
  descriptionKo: string | null
  publishedDate: string | null
  sourceLabel: string
  title: string
  titleKo: string | null
}>

export const LanguageToggle = ({ description, descriptionKo, publishedDate, sourceLabel, title, titleKo }: LanguageToggleProps): React.JSX.Element => {
  const hasTranslation = titleKo !== null || descriptionKo !== null
  const [showOriginal, setShowOriginal] = useState(false)
  const showKorean = !showOriginal
  const displayTitle = showKorean && titleKo !== null ? titleKo : title
  const displayDescription: InternalReviewDescription = showKorean && descriptionKo !== null ? { ...description, text: descriptionKo } : description
  return <>
    <header className="stack">
      <p className="page-label">{sourceLabel}</p>
      <h1>{displayTitle}</h1>
      {publishedDate === null ? null : <p className="lede">발행일 {publishedDate}</p>}
      {hasTranslation ? <button type="button" className="source-cta" onClick={() => { setShowOriginal((previous) => !previous) }}>{showKorean ? "원문 보기" : "번역 보기"}</button> : null}
    </header>
    <Description description={displayDescription} />
  </>
}

const Description = ({ description }: Readonly<{ description: InternalReviewDescription }>): React.JSX.Element => {
  const label = description.provenance === "summary" ? "자료 소개 · 요약 기반" : description.provenance === "abstract" ? "자료 소개 · 초록 기반" : "자료 소개"
  return <section className="description-card stack"><h2>{label}</h2>{description.text === null ? <p>요약 또는 초록 정보가 아직 준비되지 않았습니다.</p> : <p>{description.text}</p>}{description.truncated ? <p className="lede">검토 화면에서는 소개문을 일부만 표시합니다.</p> : null}</section>
}

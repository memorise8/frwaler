import { SITE_DOC_TYPES } from "./doc-type-map.generated"

export const DOC_TYPES = ["report", "press", "paper", "periodical", "opendata", "statistics", "research", "pdfindex", "etc"] as const
export type DocType = (typeof DOC_TYPES)[number]

const DOC_TYPE_LABELS: Readonly<Record<DocType, string>> = {
  etc: "기타",
  opendata: "공공데이터",
  paper: "논문",
  pdfindex: "PDF 검색 색인",
  periodical: "간행물",
  press: "보도자료",
  report: "보고서",
  research: "연구자료",
  statistics: "통계",
}

const isDocType = (value: string): value is DocType => (DOC_TYPES as readonly string[]).includes(value)

export const docTypeLabel = (docType: DocType): string => DOC_TYPE_LABELS[docType]

export const getDocTypeForSite = (siteId: string | null): DocType => {
  if (siteId === null) return "etc"
  const mapped = SITE_DOC_TYPES[siteId]
  return mapped !== undefined && isDocType(mapped) ? mapped : "etc"
}

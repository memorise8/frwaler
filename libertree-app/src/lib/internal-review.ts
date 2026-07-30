import { isIP } from "node:net"
import { getCategoryForSheet, siteCategoryLabel } from "./categories"

export type DescriptionProvenance = "summary" | "abstract" | "unavailable"
export type InternalReviewDescription = { readonly provenance: DescriptionProvenance; readonly text: string | null; readonly truncated: boolean }
export type InternalReviewSource = { readonly category: string; readonly continent: string; readonly country: string; readonly name: string }
export type InternalReviewListItem = { readonly description: InternalReviewDescription; readonly id: number; readonly publishedDate: string | null; readonly source: InternalReviewSource; readonly title: string }
export type InternalReviewDocument = InternalReviewListItem & { readonly authors: readonly string[]; readonly journal: string | null; readonly keywords: readonly string[]; readonly originalSourceUrl: string | null; readonly publisher: string | null }
export type InternalReviewRecord = { readonly abstract: string | null; readonly authors: string | null; readonly journal: string | null; readonly keywords: string | null; readonly metaUrl: string | null; readonly publishedDate: string | null; readonly publisher: string | null; readonly seqId: number; readonly sheet: string | null; readonly siteName: string | null; readonly siteUrl: string | null; readonly summary: string | null; readonly title: string | null }

const compact = (value: string | null): string | null => {
  const normalized = value?.replace(/\s+/g, " ").trim() ?? ""
  return normalized || null
}
const NAMED_HTML_ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  apos: "'",
  gt: ">",
  lt: "<",
  nbsp: " ",
  quot: '"',
} as const
const HTML_ENTITY_PATTERN = /&(#x[\dA-F]+|#\d+|amp|apos|gt|lt|nbsp|quot)(;|(?=[\s,.:!?)]|$))/giu
const UNRESOLVED_HTML_ENTITY_PATTERN = /&(?:#(?:x[\dA-F]+|\d+)|[a-z][a-z\d]+);/iu
const hasUnsafeMetadataMarkup = (value: string): boolean => /<!--|<\s*\/?\s*[a-z]|<\s*!doctype/iu.test(value)
const isUnicodeScalarValue = (value: number): boolean => Number.isInteger(value) && value >= 0 && value <= 0x10FFFF && (value < 0xD800 || value > 0xDFFF)
const decodeHtmlEntities = (value: string): string => {
  let decoded = value
  for (let pass = 0; pass < 3; pass += 1) {
    const next = decoded.replace(HTML_ENTITY_PATTERN, (match, entity: string): string => {
      const normalizedEntity = entity.toLowerCase()
      if (normalizedEntity.startsWith("#x")) {
        const codePoint = Number.parseInt(normalizedEntity.slice(2), 16)
        return isUnicodeScalarValue(codePoint) ? String.fromCodePoint(codePoint) : match
      }
      if (normalizedEntity.startsWith("#")) {
        const codePoint = Number.parseInt(normalizedEntity.slice(1), 10)
        return isUnicodeScalarValue(codePoint) ? String.fromCodePoint(codePoint) : match
      }
      return NAMED_HTML_ENTITIES[normalizedEntity] ?? match
    })
    if (next === decoded) break
    decoded = next
  }
  return decoded
}
const humanReadableMetadata = (value: string | null, limit: number): string | null => {
  const decoded = compact(value === null ? null : decodeHtmlEntities(value))
  return decoded === null || UNRESOLVED_HTML_ENTITY_PATTERN.test(decoded) || hasUnsafeMetadataMarkup(decoded) ? null : bounded(decoded, limit)
}
const MAX_DESCRIPTION_SOURCE_LENGTH = 8_000
const hasStructuredDescriptionDebris = (value: string): boolean =>
  value.length > MAX_DESCRIPTION_SOURCE_LENGTH
  || /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/u.test(value)
  || /<\s*\/?\s*[a-z][^>]*>/iu.test(value)
  || /&lt;\s*\/?\s*[a-z]/iu.test(value)
  || /[\[{]\s*"[^"\n]{1,80}"\s*:/u.test(value)
  || /"(?:@context|@type|itemListElement)"\s*:/iu.test(value)
  || /\b(?:function|window|document)\s*(?:\.\w+|\()/iu.test(value)
  || /^(?:[^/]{1,80}\s*\/\s*){3,}[^/]{1,200}/u.test(value)
const humanReadableDescription = (value: string | null): string | null => {
  const normalized = compact(value)
  return normalized === null || hasStructuredDescriptionDebris(normalized) ? null : normalized
}
const bounded = (value: string | null, limit: number): string | null => {
  const normalized = compact(value)
  return normalized === null ? null : normalized.length > limit ? `${normalized.slice(0, limit).trimEnd()}…` : normalized
}
const publishedDate = (value: string | null): string | null => {
  const normalized = bounded(value, 10)
  return normalized !== null && /^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(normalized) ? normalized : null
}
const descriptionFrom = (record: InternalReviewRecord, limit: number): InternalReviewDescription => {
  const summary = humanReadableDescription(record.summary)
  const abstract = humanReadableDescription(record.abstract)
  const selected = summary ?? abstract
  if (selected === null) return { provenance: "unavailable", text: null, truncated: false }
  return { provenance: summary === null ? "abstract" : "summary", text: bounded(selected, limit), truncated: selected.length > limit }
}
const splitBounded = (value: string | null): readonly string[] => {
  const normalized = humanReadableMetadata(value, 8_000)
  return normalized === null ? [] : normalized.split(/[;,]/).map((part) => bounded(part, 80)).filter((part): part is string => part !== null).slice(0, 12)
}
const isPublicHostname = (host: string): boolean => host !== "localhost" && !host.endsWith(".local") && host.includes(".") && isIP(host) === 0

export const vettedOriginalSourceUrl = (rawUrl: string | null, siteUrl: string | null): string | null => {
  const candidate = compact(rawUrl)
  const source = compact(siteUrl)
  if (candidate === null || source === null || candidate.length > 2048 || source.length > 2048) return null
  try {
    const candidateUrl = new URL(candidate)
    const sourceUrl = new URL(source)
    const sameHost = candidateUrl.hostname.toLowerCase().replace(/^www\./, "") === sourceUrl.hostname.toLowerCase().replace(/^www\./, "")
    if (candidateUrl.protocol !== "https:" || sourceUrl.protocol !== "https:" || candidateUrl.username || candidateUrl.password || sourceUrl.username || sourceUrl.password || candidateUrl.port && candidateUrl.port !== "443" || sourceUrl.port && sourceUrl.port !== "443" || !sameHost || !isPublicHostname(candidateUrl.hostname)) return null
    return candidateUrl.toString()
  } catch {
    return null
  }
}

const sourceFrom = (record: InternalReviewRecord): InternalReviewSource => {
  const taxonomy = getCategoryForSheet(record.sheet)
  return { category: siteCategoryLabel(taxonomy.category), continent: taxonomy.continent, country: taxonomy.country, name: humanReadableMetadata(record.siteName, 200) ?? "출처 미분류" }
}
export const toInternalReviewListItem = (record: InternalReviewRecord): InternalReviewListItem => ({ description: descriptionFrom(record, 360), id: record.seqId, publishedDate: publishedDate(record.publishedDate), source: sourceFrom(record), title: humanReadableMetadata(record.title, 500) ?? "(제목 없음)" })
export const toInternalReviewDocument = (record: InternalReviewRecord): InternalReviewDocument => ({ ...toInternalReviewListItem(record), authors: splitBounded(record.authors), description: descriptionFrom(record, 1200), journal: humanReadableMetadata(record.journal, 500), keywords: splitBounded(record.keywords), originalSourceUrl: vettedOriginalSourceUrl(record.metaUrl, record.siteUrl), publisher: humanReadableMetadata(record.publisher, 500) })

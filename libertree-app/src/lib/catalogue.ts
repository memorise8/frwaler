import { DatabaseSync } from "node:sqlite"
import { continentLabel, getCategoryForSheet, siteCategoryLabel } from "./categories"
import { docTypeLabel, getDocTypeForSite } from "./doc-types"
import type { DocType } from "./doc-types"
import { loadLibertreeReadOnlyDataPaths, withLibertreeReadOnlyDatabase } from "./data-path"
import { cleanSiteName, toInternalReviewDocument, toInternalReviewListItem } from "./internal-review"
import type { InternalReviewDocument, InternalReviewListItem, InternalReviewRecord } from "./internal-review"

export type CatalogueFilters = { readonly category?: string | undefined; readonly continent?: string | undefined; readonly country?: string | undefined; readonly docType?: string | undefined; readonly page?: number | undefined; readonly q?: string | undefined; readonly siteId?: string | undefined }
export type CatalogueResult = { readonly items: readonly InternalReviewListItem[]; readonly page: number; readonly total: number }
export type CatalogueBrowseBucket = { readonly count: number; readonly key: string; readonly label: string }
export type CatalogueBrowseSummary = {
  readonly categories: readonly CatalogueBrowseBucket[]
  readonly continents: readonly CatalogueBrowseBucket[]
  readonly countries: readonly CatalogueBrowseBucket[]
  readonly docTypes: readonly CatalogueBrowseBucket[]
  readonly sourceCount: number
  readonly totalDocuments: number
}
type QueryRow = Readonly<Record<string, unknown>>
type BlobAuthorization = { readonly originalFilename: string | null; readonly available: boolean }

const PAGE_SIZE = 20
const MAX_PAGE = 10_000
const MAX_QUERY_LENGTH = 500

const value = (row: QueryRow, name: string): string | null => typeof row[name] === "string" ? row[name] : null
const integer = (row: QueryRow, name: string): number | null => typeof row[name] === "number" && Number.isSafeInteger(row[name]) ? row[name] : null
const normalizePage = (input: number | undefined): number => Number.isSafeInteger(input) ? Math.max(1, Math.min(MAX_PAGE, input ?? 1)) : 1
const normalizedQueryTokens = (query: string | undefined): readonly string[] => (query ?? "").trim().slice(0, MAX_QUERY_LENGTH).split(/\s+/).filter(Boolean).slice(0, 12)
const compareBuckets = (left: CatalogueBrowseBucket, right: CatalogueBrowseBucket): number => right.count - left.count || left.label.localeCompare(right.label, "ko")

const withDatabase = <T>(operation: (database: DatabaseSync) => T): T => {
  return withLibertreeReadOnlyDatabase(process.env, operation)
}

export const verifyCatalogueReadiness = (): void => {
  withDatabase((database) => {
    database.prepare("SELECT 1 FROM documents LIMIT 1").get()
  })
}

const bucketKeyFor = (row: QueryRow, key: "category" | "continent" | "country" | "docType"): string =>
  key === "docType" ? getDocTypeForSite(value(row, "site_id")) : getCategoryForSheet(value(row, "sheet"))[key]

const bucketLabelFor = (bucketKey: string, key: "category" | "continent" | "country" | "docType"): string => {
  if (key === "continent") return continentLabel(bucketKey as ReturnType<typeof getCategoryForSheet>["continent"])
  if (key === "category") return siteCategoryLabel(bucketKey as ReturnType<typeof getCategoryForSheet>["category"])
  if (key === "docType") return docTypeLabel(bucketKey as DocType)
  return bucketKey
}

const aggregateBuckets = (rows: readonly QueryRow[], key: "category" | "continent" | "country" | "docType"): readonly CatalogueBrowseBucket[] => {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const bucketKey = bucketKeyFor(row, key)
    counts.set(bucketKey, (counts.get(bucketKey) ?? 0) + (integer(row, "documents") ?? 0))
  }
  return [...counts].map(([bucketKey, count]) => ({ count, key: bucketKey, label: bucketLabelFor(bucketKey, key) })).filter((bucket) => bucket.count > 0).sort(compareBuckets)
}

export const getCatalogueBrowseSummary = (): CatalogueBrowseSummary => withDatabase((database) => {
  const totals = database.prepare("SELECT COUNT(*) AS documents, COUNT(DISTINCT site_id) AS sources FROM documents").get()
  const rows = database.prepare("SELECT d.site_id, s.sheet, COUNT(*) AS documents FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id GROUP BY d.site_id, s.sheet").all()
  const totalRow: QueryRow = totals ?? {}
  return {
    categories: aggregateBuckets(rows, "category"),
    continents: aggregateBuckets(rows, "continent"),
    countries: aggregateBuckets(rows, "country"),
    docTypes: aggregateBuckets(rows, "docType"),
    sourceCount: integer(totalRow, "sources") ?? 0,
    totalDocuments: integer(totalRow, "documents") ?? 0,
  }
})

const matchesTaxonomy = (filters: CatalogueFilters, sheet: string | null, siteId: string | null): boolean => {
  const taxonomy = getCategoryForSheet(sheet)
  return (filters.category === undefined || taxonomy.category === filters.category)
    && (filters.continent === undefined || taxonomy.continent === filters.continent)
    && (filters.country === undefined || taxonomy.country === filters.country)
    && (filters.docType === undefined || getDocTypeForSite(siteId) === filters.docType)
}

// Intermediate "국가·지역" facet: countries within the active browse scope (e.g. within a doc type),
// enabling a 유형 → 나라 → 사이트 drill-down. The country filter itself is ignored here.
export const getCatalogueScopedCountries = (filters: CatalogueFilters): readonly CatalogueBrowseBucket[] => withDatabase((database) => {
  const rows = database.prepare("SELECT d.site_id, s.sheet, COUNT(*) AS documents FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id GROUP BY d.site_id, s.sheet").all()
  const counts = new Map<string, number>()
  for (const row of rows) {
    const siteId = value(row, "site_id")
    const count = integer(row, "documents") ?? 0
    if (siteId === null || count === 0 || !matchesTaxonomy({ ...filters, country: undefined }, value(row, "sheet"), siteId)) continue
    const country = getCategoryForSheet(value(row, "sheet")).country
    counts.set(country, (counts.get(country) ?? 0) + count)
  }
  return [...counts].map(([key, count]) => ({ count, key, label: key })).filter((bucket) => bucket.count > 0).sort(compareBuckets)
})

// Secondary "수집 출처(사이트)" facet: sites within the active browse scope, each drillable via siteId.
export const getCatalogueSiteSources = (filters: CatalogueFilters): readonly CatalogueBrowseBucket[] => withDatabase((database) => {
  const rows = database.prepare("SELECT d.site_id, s.sheet, s.site_name, s.site_url, COUNT(*) AS documents FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id GROUP BY d.site_id, s.sheet, s.site_name, s.site_url").all()
  return rows.flatMap((row) => {
    const siteId = value(row, "site_id")
    const count = integer(row, "documents") ?? 0
    if (siteId === null || count === 0 || !matchesTaxonomy(filters, value(row, "sheet"), siteId)) return []
    return [{ count, key: siteId, label: cleanSiteName(value(row, "site_name"), value(row, "site_url")) }]
  }).sort(compareBuckets)
})

const recordFrom = (row: QueryRow): InternalReviewRecord | null => {
  const seqId = integer(row, "seq_id")
  if (seqId === null || seqId < 0 || seqId >= 1e12) return null
  return {
    abstract: value(row, "abstract"), authors: value(row, "authors"), journal: value(row, "journal"), keywords: value(row, "keywords"), metaUrl: value(row, "meta_url"), publishedDate: value(row, "published_date"), publisher: value(row, "publisher"), seqId, sheet: value(row, "sheet"), siteId: value(row, "site_id"), siteName: value(row, "site_name"), siteUrl: value(row, "site_url"), summary: value(row, "summary"), title: value(row, "title"),
  }
}

const SELECT_REVIEW = `
  d.seq_id, d.title, d.authors, d.publisher, d.journal, d.published_date, d.keywords,
  substr(d.summary, 1, 1201) AS summary, substr(d.abstract, 1, 1201) AS abstract,
  d.meta_url, d.site_id, s.site_name, s.site_url, s.sheet
`

export const searchCatalogue = (filters: CatalogueFilters): CatalogueResult => withDatabase((database) => {
  const page = normalizePage(filters.page)
  const where: string[] = []
  const parameters: unknown[] = []
  for (const token of normalizedQueryTokens(filters.q)) {
    const pattern = `%${token}%`
    where.push("(COALESCE(d.title, '') LIKE ? OR COALESCE(d.summary, '') LIKE ? OR COALESCE(d.abstract, '') LIKE ? OR COALESCE(d.authors, '') LIKE ? OR COALESCE(d.keywords, '') LIKE ?)")
    parameters.push(pattern, pattern, pattern, pattern, pattern)
  }
  if (filters.siteId !== undefined) {
    where.push("d.site_id = ?")
    parameters.push(filters.siteId)
  }
  if (filters.category !== undefined || filters.continent !== undefined || filters.country !== undefined || filters.docType !== undefined) {
    const matchingSiteIds = database.prepare("SELECT site_id, sheet FROM sites").all().flatMap((row) => {
      const siteId = value(row, "site_id")
      return siteId !== null && matchesTaxonomy(filters, value(row, "sheet"), siteId) ? [siteId] : []
    })
    if (matchingSiteIds.length === 0) return { items: [], page, total: 0 }
    where.push(`d.site_id IN (${matchingSiteIds.map(() => "?").join(",")})`)
    parameters.push(...matchingSiteIds)
  }
  const whereSql = where.length === 0 ? "" : `WHERE ${where.join(" AND ")}`
  const countRow = database.prepare(`SELECT COUNT(*) AS total FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id ${whereSql}`).get(...parameters)
  const total = countRow === undefined ? 0 : integer(countRow, "total") ?? 0
  const rows = database.prepare(`SELECT ${SELECT_REVIEW} FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id ${whereSql} ORDER BY d.published_date DESC, d.seq_id DESC LIMIT ? OFFSET ?`).all(...parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
  const items = rows.map(recordFrom).filter((record): record is InternalReviewRecord => record !== null).map(toInternalReviewListItem)
  return { items, page, total }
})

type KoreanTranslations = { readonly description: string | null; readonly title: string | null }

const loadKoreanTranslations = (database: DatabaseSync, seqId: number): KoreanTranslations => {
  const rows = database.prepare(
    `SELECT source_field, translation_text FROM document_translations
     WHERE seq_id = ? AND target_locale = 'ko-KR' AND state = 'completed' AND source_field IN ('title', 'description')
     ORDER BY completed_at DESC, translation_id DESC`,
  ).all(seqId)
  let title: string | null = null
  let description: string | null = null
  for (const row of rows) {
    const field = value(row, "source_field")
    if (field === "title" && title === null) title = value(row, "translation_text")
    if (field === "description" && description === null) description = value(row, "translation_text")
  }
  return { description, title }
}

export const getCatalogueDocument = (seqId: number): InternalReviewDocument | null => {
  if (!Number.isSafeInteger(seqId) || seqId < 0 || seqId >= 1e12) return null
  return withDatabase((database) => {
    const row = database.prepare(`SELECT ${SELECT_REVIEW} FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id WHERE d.seq_id = ?`).get(seqId)
    const record = row === undefined ? null : recordFrom(row)
    if (record === null) return null
    const translations = loadKoreanTranslations(database, seqId)
    return toInternalReviewDocument({ ...record, descriptionKo: translations.description, titleKo: translations.title })
  })
}

export const getBlobAuthorization = (seqId: number, extension: "pdf" | "txt"): BlobAuthorization | null => {
  if (!Number.isSafeInteger(seqId) || seqId < 0 || seqId >= 1e12) return null
  return withDatabase((database) => {
    const row = database.prepare("SELECT original_filename, pdf_downloaded, text_extracted FROM documents WHERE seq_id = ?").get(seqId)
    if (row === undefined) return null
    const downloaded = integer(row, extension === "pdf" ? "pdf_downloaded" : "text_extracted") === 1
    return { available: downloaded, originalFilename: value(row, "original_filename") }
  })
}

export const blobPathFor = (seqId: number, extension: "pdf" | "txt"): string => {
  const paths = loadLibertreeReadOnlyDataPaths(process.env)
  const filename = seqId.toString().padStart(12, "0")
  return `${paths.blobRoot}/${filename.slice(0, 4)}/${filename.slice(4, 8)}/${filename}.${extension}`
}

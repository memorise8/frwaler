import type { DatabaseSync } from "node:sqlite"
import { getCategoryForSheet } from "./categories"
import { getDocTypeForSite, docTypeLabel } from "./doc-types"
import type { DocType } from "./doc-types"
import { withLibertreeReadOnlyDatabase } from "./data-path"
import { cleanSiteName } from "./internal-review"

export type ReportBucket = { readonly key: string; readonly recs: number; readonly pdf: number; readonly rate: number; readonly sites: number }
export type UnmetTypeRow = { readonly key: string; readonly unmet: number; readonly noUrl: number; readonly download: number }
export type WeakSite = { readonly name: string; readonly country: string; readonly docType: string; readonly recs: number; readonly pdf: number; readonly rate: number }
export type CollectionReport = {
  readonly total: { readonly recs: number; readonly pdf: number; readonly txt: number; readonly ko: number; readonly summary: number; readonly sizeTb: number; readonly sites: number; readonly rate: number }
  readonly byDocType: readonly ReportBucket[]
  readonly byCountry: readonly ReportBucket[]
  readonly unmet: { readonly total: number; readonly noUrl: number; readonly hasUrl: number; readonly a: number; readonly b: number; readonly c: number; readonly byType: readonly UnmetTypeRow[] }
  readonly weakSites: readonly WeakSite[]
}

type Row = Readonly<Record<string, unknown>>
const str = (row: Row, name: string): string | null => typeof row[name] === "string" ? (row[name] as string) : null
const int = (row: Row, name: string): number => typeof row[name] === "number" && Number.isFinite(row[name]) ? (row[name] as number) : 0
const rate = (pdf: number, recs: number): number => recs > 0 ? Math.round((1000 * pdf) / recs) / 10 : 0

const AGG_SQL = `
  SELECT d.site_id AS site_id, s.sheet AS sheet, s.site_name AS site_name, s.site_url AS site_url,
    COUNT(*) AS recs,
    SUM(CASE WHEN d.pdf_downloaded = 1 THEN 1 ELSE 0 END) AS pdf,
    SUM(CASE WHEN d.text_extracted = 1 THEN 1 ELSE 0 END) AS txt,
    SUM(CASE WHEN COALESCE(d.pdf_downloaded, 0) <> 1 AND (d.pdf_url IS NULL OR d.pdf_url = '') THEN 1 ELSE 0 END) AS un_nourl,
    SUM(CASE WHEN COALESCE(d.pdf_downloaded, 0) <> 1 AND d.pdf_url IS NOT NULL AND d.pdf_url <> '' THEN 1 ELSE 0 END) AS un_hasurl,
    CAST(SUM(COALESCE(d.pdf_size_bytes, 0)) AS REAL) AS bytes
  FROM documents d LEFT JOIN sites s ON s.site_id = d.site_id
  GROUP BY d.site_id, s.sheet, s.site_name, s.site_url
`

type SiteAgg = { recs: number; pdf: number; txt: number; noUrl: number; hasUrl: number }
const emptyAgg = (): SiteAgg => ({ recs: 0, pdf: 0, txt: 0, noUrl: 0, hasUrl: 0 })
const add = (target: SiteAgg, row: Row): void => {
  target.recs += int(row, "recs"); target.pdf += int(row, "pdf"); target.txt += int(row, "txt")
  target.noUrl += int(row, "un_nourl"); target.hasUrl += int(row, "un_hasurl")
}

const buckets = (map: Map<string, SiteAgg & { sites: number }>): readonly ReportBucket[] =>
  [...map].map(([key, v]) => ({ key, recs: v.recs, pdf: v.pdf, rate: rate(v.pdf, v.recs), sites: v.sites }))
    .filter((b) => b.recs > 0).sort((l, r) => r.recs - l.recs)

export const getCollectionReport = (): CollectionReport => withLibertreeReadOnlyDatabase(process.env, (database: DatabaseSync) => {
  const rows = database.prepare(AGG_SQL).all()
  const total = { ...emptyAgg(), bytes: 0, sites: 0 }
  const byType = new Map<DocType, SiteAgg & { sites: number }>()
  const byCountry = new Map<string, SiteAgg & { sites: number }>()
  const unmetByType = new Map<DocType, { noUrl: number; hasUrl: number }>()
  const sites: WeakSite[] = []
  let a = 0
  let b = 0

  for (const row of rows) {
    const siteId = str(row, "site_id")
    const recs = int(row, "recs")
    if (recs === 0) continue
    const dt = getDocTypeForSite(siteId)
    const country = getCategoryForSheet(str(row, "sheet")).country
    const noUrl = int(row, "un_nourl")
    const hasUrl = int(row, "un_hasurl")

    add(total, row); total.bytes += int(row, "bytes"); total.sites += 1
    for (const [map, key] of [[byType, dt] as const, [byCountry, country] as const]) {
      const entry = (map as Map<string, SiteAgg & { sites: number }>).get(key) ?? { ...emptyAgg(), sites: 0 }
      add(entry, row); entry.sites += 1
      ;(map as Map<string, SiteAgg & { sites: number }>).set(key, entry)
    }
    const um = unmetByType.get(dt) ?? { noUrl: 0, hasUrl: 0 }
    um.noUrl += noUrl; um.hasUrl += hasUrl; unmetByType.set(dt, um)
    // A = 원문 부재(보도자료 링크없음), B = 문서형 링크없음
    if (dt === "press") a += noUrl
    else b += noUrl

    const pdf = int(row, "pdf")
    sites.push({ name: cleanSiteName(str(row, "site_name"), str(row, "site_url")), country, docType: docTypeLabel(dt), recs, pdf, rate: rate(pdf, recs) })
  }

  const ko = int(database.prepare("SELECT COUNT(DISTINCT seq_id) AS n FROM document_translations WHERE target_locale = 'ko-KR' AND state = 'completed'").get() ?? {}, "n")
  const summary = int(database.prepare("SELECT COUNT(*) AS n FROM documents WHERE summary IS NOT NULL AND summary <> ''").get() ?? {}, "n")
  const c = total.hasUrl

  return {
    total: { recs: total.recs, pdf: total.pdf, txt: total.txt, ko, summary, sizeTb: Math.round(total.bytes / 1e10) / 100, sites: total.sites, rate: rate(total.pdf, total.recs) },
    byDocType: [...byType].map(([key, v]) => ({ key: docTypeLabel(key), recs: v.recs, pdf: v.pdf, rate: rate(v.pdf, v.recs), sites: v.sites })).filter((x) => x.recs > 0).sort((l, r) => r.recs - l.recs),
    byCountry: buckets(byCountry),
    unmet: {
      total: total.noUrl + total.hasUrl, noUrl: total.noUrl, hasUrl: total.hasUrl, a, b, c,
      byType: [...unmetByType].map(([key, v]) => ({ key: docTypeLabel(key), unmet: v.noUrl + v.hasUrl, noUrl: v.noUrl, download: v.hasUrl })).filter((x) => x.unmet > 0).sort((l, r) => r.unmet - l.unmet),
    },
    weakSites: sites.filter((s) => s.recs >= 1000 && s.rate < 30).sort((l, r) => l.rate - r.rate).slice(0, 8),
  }
})

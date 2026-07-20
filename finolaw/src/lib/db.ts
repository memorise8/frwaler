import fs from "fs";
import Database from "better-sqlite3";
import path from "path";
import { cached } from "./cache";
import {
  ALL_CATEGORIES,
  ALL_CONTINENTS,
  classificationStats,
  getCategoryForSheet,
  type Continent,
  type SiteFunctionCategory,
} from "./categories";

const DB_PATH = path.join(process.cwd(), "..", "data", "libertree.db");

// ====================================================================
// Legacy `Paper` shape kept for existing pages — adapted from the
// libertree `documents` table. Metadata-derived fields (category,
// docType, doi, etc.) are no longer available so they're typed as
// nullable and populated as null. Pages that referenced them have
// been updated to omit / fallback gracefully.
// ====================================================================

export interface Paper {
  id: string;
  site_id: string;
  external_id: string;
  title: string | null;
  authors: string | null;
  abstract: string | null;
  category: string | null;
  keywords: string | null;
  published_date: string | null;
  url: string | null;
  pdf_url: string | null;
  doi: string | null;
  department: string | null;
  metadata: string | null;
  crawled_at: string;
}

// Kept as a no-op shape so legacy callers still type-check.
export interface PaperMetadata {
  documentNumber?: string;
  documentTypeName?: string;
  replyReference?: string;
  relatedLaws?: string[];
  trialHistory?: string[];
  referencedCases?: string[];
  citedCases?: string[];
  relatedTopics?: string[];
  attachedFiles?: Array<{ name?: string; url?: string }> | string[];
  attrYr?: string;
  rawHtmlPath?: string;
  firstRegDtm?: string;
  lastAltDtm?: string;
  [key: string]: unknown;
}

export interface DocTypeCount {
  documentTypeName: string;
  count: number;
}

export interface SiteCount {
  site_id: string;
  site_name: string;
  count: number;
  last_crawled: string | null;
}

export interface SiteOption {
  site_id: string;
  site_name: string;
}

export interface DbState {
  kind: "ready" | "missing" | "incomplete";
  path: string;
  missingTables?: string[];
}

function getDb() {
  return new Database(DB_PATH, { readonly: true, fileMustExist: true });
}

function getMissingTables(
  db: ReturnType<typeof getDb>,
  tables: readonly string[]
): string[] {
  if (tables.length === 0) return [];

  const placeholders = tables.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT name FROM sqlite_master
       WHERE type IN ('table', 'view') AND name IN (${placeholders})`
    )
    .all(...tables) as { name: string }[];
  const present = new Set(rows.map((row) => row.name));

  return tables.filter((table) => !present.has(table));
}

function hasTables(
  db: ReturnType<typeof getDb>,
  tables: readonly string[]
): boolean {
  return getMissingTables(db, tables).length === 0;
}

export function getDbState(requiredTables: readonly string[] = []): DbState {
  if (!fs.existsSync(DB_PATH)) {
    return { kind: "missing", path: DB_PATH };
  }

  if (requiredTables.length === 0) {
    return { kind: "ready", path: DB_PATH };
  }

  const db = getDb();
  try {
    const missingTables = getMissingTables(db, requiredTables);
    if (missingTables.length > 0) {
      return {
        kind: "incomplete",
        path: DB_PATH,
        missingTables,
      };
    }

    return { kind: "ready", path: DB_PATH };
  } finally {
    db.close();
  }
}

/**
 * Legacy metadata parser. The libertree schema has no `metadata` column,
 * so the input is always null and we return {} — kept exported so existing
 * pages keep type-checking without rewriting them.
 */
export function parseMetadata(raw: string | null): PaperMetadata {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as PaperMetadata;
  } catch {
    return {};
  }
}

export interface SearchFilters {
  siteIds?: string[];
  q?: string;
  category?: string;
  docType?: string;
  dateFrom?: string;
  dateTo?: string;
  page?: number;
  pageSize?: number;
}

export interface SearchResult {
  papers: Paper[];
  total: number;
  page: number;
  pageSize: number;
}

function emptySearchResult(filters: SearchFilters): SearchResult {
  return {
    papers: [],
    total: 0,
    page: filters.page ?? 1,
    pageSize: filters.pageSize ?? 20,
  };
}

/**
 * Sanitize a user query for FTS5 MATCH.
 * Each whitespace-separated token is wrapped in double-quotes (internal `"`
 * are escaped as `""`).  Returns null if there are no usable tokens, which
 * tells the caller to fall back to the non-FTS path.
 */
export function sanitizeFtsQuery(q: string): string | null {
  const tokens = q
    .trim()
    .split(/\s+/)
    .filter((t) => t.length > 0)
    .map((t) => `"${t.replace(/"/g, '""')}"`);
  return tokens.length > 0 ? tokens.join(" ") : null;
}

const TTL = 5 * 60 * 1000; // 5 minutes

function getDefaultSiteIds(db: ReturnType<typeof getDb>): string[] {
  const rows = db
    .prepare(
      `SELECT DISTINCT site_id
       FROM documents
       WHERE site_id IS NOT NULL AND site_id != ''
       ORDER BY site_id`
    )
    .all() as { site_id: string }[];
  return rows.map((row) => row.site_id);
}

// ====================================================================
// libertree blob layout
//   12-digit zero-padded seq_id, two-level sharding under libertree/:
//     libertree/{XXXX}/{YYYY}/{XXXXYYYYZZZZ}.{pdf|txt}
//   Matches crawler/blob_storage.py.
// ====================================================================

export function blobPath(seqId: number, ext: "pdf" | "txt"): string {
  if (!Number.isInteger(seqId) || seqId < 0 || seqId >= 1e12) {
    throw new RangeError(`blobPath: seq_id out of range [0, 1e12): ${seqId}`);
  }
  const name = seqId.toString().padStart(12, "0");
  return `libertree/${name.slice(0, 4)}/${name.slice(4, 8)}/${name}.${ext}`;
}

// ====================================================================
// Paper-shape adapter over the libertree `documents` table.
// Returns rows shaped like the legacy `Paper` interface so existing
// pages keep rendering. Metadata-derived fields (category/docType/doi)
// are populated as null since the column no longer exists.
// ====================================================================

interface DocRow {
  seq_id: number;
  collected_at: string;
  site_id: string;
  post_number: string | null;
  meta_url: string | null;
  title: string | null;
  published_date: string | null;
  listed_date: string | null;
  authors: string | null;
  publisher: string | null;
  journal: string | null;
  pdf_url: string | null;
  keywords: string | null;
  abstract: string | null;
  original_filename: string | null;
  pdf_downloaded: number | null;
  text_extracted: number | null;
  pdf_size_bytes: number | null;
  pdf_sha256: string | null;
  summary: string | null;
  summary_model: string | null;
  summary_at: string | null;
}

function documentToPaperShape(d: DocRow): Paper {
  return {
    id: String(d.seq_id),
    site_id: d.site_id,
    external_id: d.post_number ?? "",
    title: d.title,
    authors: d.authors,
    abstract: d.abstract,
    category: null,
    keywords: d.keywords,
    published_date: d.published_date,
    url: d.meta_url,
    pdf_url: d.pdf_url,
    doi: null,
    department: d.publisher,
    metadata: null,
    crawled_at: d.collected_at,
  };
}

const DOC_COLS_FULL = `
  seq_id, collected_at, site_id, post_number, meta_url, title,
  published_date, listed_date, authors, publisher, journal,
  pdf_url, keywords, abstract, original_filename,
  pdf_downloaded, text_extracted, pdf_size_bytes, pdf_sha256,
  summary, summary_model, summary_at
`;

function normalizeSiteIds(
  db: ReturnType<typeof getDb>,
  siteIds?: string[]
): string[] {
  if (siteIds && siteIds.length > 0) {
    return [...siteIds];
  }
  return getDefaultSiteIds(db);
}

export function searchPapers(filters: SearchFilters): SearchResult {
  if (getDbState(["documents"]).kind !== "ready") {
    return emptySearchResult(filters);
  }

  const db = getDb();
  try {
    const resolvedSiteIds = normalizeSiteIds(db, filters.siteIds);
    const {
      q,
      dateFrom,
      dateTo,
      page = 1,
      pageSize = 20,
    } = filters;

    const offset = (page - 1) * pageSize;

    // FTS5 over documents_fts (built by scripts/migrate_documents_fts.py).
    // Currently not present in libertree.db — the LIKE fallback below
    // handles all queries until that migration runs.
    const rawTokens = q?.trim().split(/\s+/).filter((t) => t.length > 0) ?? [];
    const ftsEligible =
      rawTokens.length > 0 &&
      rawTokens.every((t) => t.length >= 3) &&
      hasTables(db, ["documents_fts"]);
    const ftsQuery = ftsEligible ? sanitizeFtsQuery(q!) : null;

    if (ftsQuery) {
      const ftsWhere: string[] = [];
      const ftsParams: unknown[] = [ftsQuery];
      if (resolvedSiteIds.length > 0) {
        ftsWhere.push(
          `d.site_id IN (${resolvedSiteIds.map(() => "?").join(",")})`,
        );
        ftsParams.push(...resolvedSiteIds);
      }
      if (dateFrom) {
        ftsWhere.push(`d.published_date >= ?`);
        ftsParams.push(dateFrom);
      }
      if (dateTo) {
        ftsWhere.push(`d.published_date <= ?`);
        ftsParams.push(dateTo);
      }
      const ftsExtra = ftsWhere.length ? `AND ${ftsWhere.join(" AND ")}` : "";

      const total = (
        db
          .prepare(
            `SELECT COUNT(*) as c
             FROM documents d
             JOIN documents_fts f ON d.rowid = f.rowid
             WHERE documents_fts MATCH ?
             ${ftsExtra}`,
          )
          .get(...ftsParams) as { c: number }
      ).c;

      const rows = db
        .prepare(
          `SELECT d.*
           FROM documents d
           JOIN documents_fts f ON d.rowid = f.rowid
           WHERE documents_fts MATCH ?
           ${ftsExtra}
           ORDER BY d.published_date DESC, d.collected_at DESC
           LIMIT ? OFFSET ?`,
        )
        .all(...ftsParams, pageSize, offset) as DocRow[];

      return {
        papers: rows.map(documentToPaperShape),
        total,
        page,
        pageSize,
      };
    }

    // Fallback: LIKE-based search (no q, short tokens, or FTS table absent).
    const where: string[] = [];
    const params: unknown[] = [];
    const tokens = q?.trim().split(/\s+/).filter((token) => token.length > 0) ?? [];

    if (tokens.length > 0) {
      tokens.forEach((token) => {
        const pattern = `%${token}%`;
        where.push(
          `(COALESCE(title, '') LIKE ? OR COALESCE(abstract, '') LIKE ? OR COALESCE(keywords, '') LIKE ?)`
        );
        params.push(pattern, pattern, pattern);
      });
    }

    if (resolvedSiteIds.length > 0) {
      where.push(`site_id IN (${resolvedSiteIds.map(() => "?").join(",")})`);
      params.push(...resolvedSiteIds);
    }
    if (dateFrom) {
      where.push(`published_date >= ?`);
      params.push(dateFrom);
    }
    if (dateTo) {
      where.push(`published_date <= ?`);
      params.push(dateTo);
    }

    const whereSQL = where.length ? `WHERE ${where.join(" AND ")}` : "";

    const total = (
      db
        .prepare(`SELECT COUNT(*) as c FROM documents ${whereSQL}`)
        .get(...params) as { c: number }
    ).c;

    const rows = db
      .prepare(
        `SELECT ${DOC_COLS_FULL} FROM documents ${whereSQL}
         ORDER BY published_date DESC, collected_at DESC LIMIT ? OFFSET ?`
      )
      .all(...params, pageSize, offset) as DocRow[];

    return {
      papers: rows.map(documentToPaperShape),
      total,
      page,
      pageSize,
    };
  } finally {
    db.close();
  }
}

export function getPaper(id: string): Paper | null {
  const state = getDbState();
  if (state.kind !== "ready") return null;

  const db = getDb();
  try {
    // libertree IDs are numeric seq_id. Non-numeric IDs (legacy UUIDs from
    // the old papers.db) are not supported — there's no papers table here.
    if (!/^\d+$/.test(id)) return null;
    if (!hasTables(db, ["documents"])) return null;
    const row = db
      .prepare(`SELECT ${DOC_COLS_FULL} FROM documents WHERE seq_id = ?`)
      .get(Number(id)) as DocRow | undefined;
    return row ? documentToPaperShape(row) : null;
  } finally {
    db.close();
  }
}

/**
 * libertree has no `documentTypeName` metadata column. Returned empty
 * for legacy compatibility — pages that rendered this filter degrade
 * gracefully to an empty dropdown.
 */
export function getDocTypeCounts(_siteIds?: string[]): DocTypeCount[] {
  void _siteIds;
  return [];
}

/**
 * libertree has no `category` metadata column. Returned empty for
 * legacy compatibility.
 */
export function getCategories(_siteIds?: string[]): string[] {
  void _siteIds;
  return [];
}

export interface DbStats {
  totalPapers: number;
  pdfPapers: number;
  txtPapers: number;
  totalSites: number;
  registeredSites: number;
  lastCrawled: string | null;
}

export function getDbStats(): DbStats {
  if (getDbState(["documents"]).kind !== "ready") {
    return {
      totalPapers: 0,
      pdfPapers: 0,
      txtPapers: 0,
      totalSites: 0,
      registeredSites: 0,
      lastCrawled: null,
    };
  }

  return cached("stats", TTL, () => {
    const db = getDb();
    try {
      const totals = db
        .prepare(
          `SELECT
             COUNT(*) AS total,
             SUM(CASE WHEN pdf_downloaded = 1 THEN 1 ELSE 0 END) AS pdf,
             SUM(CASE WHEN text_extracted = 1 THEN 1 ELSE 0 END) AS txt,
             COUNT(DISTINCT site_id) AS sites,
             MAX(collected_at) AS last
           FROM documents`,
        )
        .get() as {
          total: number;
          pdf: number | null;
          txt: number | null;
          sites: number;
          last: string | null;
        };
      const registeredSites = hasTables(db, ["sites"])
        ? (db.prepare(`SELECT COUNT(*) as c FROM sites`).get() as {
            c: number;
          }).c
        : 0;
      return {
        totalPapers: totals.total,
        pdfPapers: totals.pdf ?? 0,
        txtPapers: totals.txt ?? 0,
        totalSites: totals.sites,
        registeredSites,
        lastCrawled: totals.last,
      };
    } finally {
      db.close();
    }
  });
}

// ====================================================================
// Global dashboard aggregations — group documents by sheet (via the
// `sites` table) and roll up to continent / country / function-category
// using lib/categories.ts. All counts come from a single JOIN so the
// numbers are internally consistent.
// ====================================================================

export interface DashboardKPI {
  totalDocs: number;
  totalSites: number;
  registeredSites: number;
  pdfDocs: number;
  txtDocs: number;
  summarizedDocs: number;
  lastCrawled: string | null;
}

export interface DashboardGroupRow {
  /** Group label (e.g. "Europe", "프랑스", "Government"). */
  label: string;
  /** Distinct sites contributing to this group. */
  sites: number;
  /** Total docs aggregated in this group. */
  docs: number;
  pdf: number;
  txt: number;
  summarized: number;
}

export interface DashboardRecentSite {
  site_id: string;
  site_name: string;
  sheet: string | null;
  country: string;
  continent: string;
  category: string;
  docs: number;
  last_crawled: string | null;
}

export interface DashboardStats {
  kpi: DashboardKPI;
  byContinent: DashboardGroupRow[];
  byCountry: DashboardGroupRow[];
  byCategory: DashboardGroupRow[];
  bySheet: DashboardGroupRow[];
  recentSites: DashboardRecentSite[];
  classification: {
    totalSheets: number;
    mappedSheets: number;
    unmappedSheets: number;
    unmappedList: string[];
  };
}

interface SheetRollupRow {
  site_id: string;
  site_name: string;
  sheet: string | null;
  total: number;
  pdf: number;
  txt: number;
  summarized: number;
  last_crawled: string | null;
}

export function getDashboardStats(): DashboardStats {
  if (getDbState(["documents"]).kind !== "ready") {
    return {
      kpi: {
        totalDocs: 0,
        totalSites: 0,
        registeredSites: 0,
        pdfDocs: 0,
        txtDocs: 0,
        summarizedDocs: 0,
        lastCrawled: null,
      },
      byContinent: [],
      byCountry: [],
      byCategory: [],
      bySheet: [],
      recentSites: [],
      classification: {
        totalSheets: 0,
        mappedSheets: 0,
        unmappedSheets: 0,
        unmappedList: [],
      },
    };
  }

  return cached("dashboardStats", TTL, () => {
    const db = getDb();
    try {
      const stats = getDbStats();
      const hasSites = hasTables(db, ["sites"]);
      const selectSheet = hasSites ? "s.sheet" : "NULL";
      const selectName = hasSites ? "COALESCE(s.site_name, d.site_id)" : "d.site_id";
      const joinClause = hasSites ? "LEFT JOIN sites s ON s.site_id = d.site_id" : "";

      const perSite = db
        .prepare(
          `SELECT d.site_id           AS site_id,
                  ${selectName}        AS site_name,
                  ${selectSheet}       AS sheet,
                  COUNT(d.seq_id)      AS total,
                  SUM(CASE WHEN d.pdf_downloaded = 1 THEN 1 ELSE 0 END) AS pdf,
                  SUM(CASE WHEN d.text_extracted = 1 THEN 1 ELSE 0 END) AS txt,
                  SUM(CASE WHEN d.summary IS NOT NULL AND d.summary != '' THEN 1 ELSE 0 END) AS summarized,
                  MAX(d.collected_at)  AS last_crawled
           FROM documents d
           ${joinClause}
           GROUP BY d.site_id`,
        )
        .all() as SheetRollupRow[];

      // accumulate by group label
      function mkRow(label: string): DashboardGroupRow {
        return { label, sites: 0, docs: 0, pdf: 0, txt: 0, summarized: 0 };
      }
      const continentMap = new Map<string, DashboardGroupRow>();
      const countryMap = new Map<string, DashboardGroupRow>();
      const categoryMap = new Map<string, DashboardGroupRow>();
      const sheetMap = new Map<string, DashboardGroupRow>();

      for (const row of perSite) {
        const cat = getCategoryForSheet(row.sheet);
        const targets = [
          continentMap.get(cat.continent) ?? continentMap.set(cat.continent, mkRow(cat.continent)).get(cat.continent)!,
          countryMap.get(cat.country) ?? countryMap.set(cat.country, mkRow(cat.country)).get(cat.country)!,
          categoryMap.get(cat.category) ?? categoryMap.set(cat.category, mkRow(cat.category)).get(cat.category)!,
          sheetMap.get(cat.sheet) ?? sheetMap.set(cat.sheet, mkRow(cat.sheet)).get(cat.sheet)!,
        ];
        for (const t of targets) {
          t.sites += 1;
          t.docs += row.total | 0;
          t.pdf += (row.pdf ?? 0) | 0;
          t.txt += (row.txt ?? 0) | 0;
          t.summarized += (row.summarized ?? 0) | 0;
        }
      }

      const byContinent = ALL_CONTINENTS
        .map((c) => continentMap.get(c))
        .filter((r): r is DashboardGroupRow => !!r && r.docs > 0)
        .sort((a, b) => b.docs - a.docs);

      // include any extra continents that slipped through (shouldn't happen
      // since getCategoryForSheet only returns ALL_CONTINENTS values).
      for (const [k, v] of continentMap) {
        if (!ALL_CONTINENTS.includes(k as Continent) && v.docs > 0) byContinent.push(v);
      }

      const byCategory = ALL_CATEGORIES
        .map((c) => categoryMap.get(c))
        .filter((r): r is DashboardGroupRow => !!r && r.docs > 0)
        .sort((a, b) => b.docs - a.docs);
      for (const [k, v] of categoryMap) {
        if (!ALL_CATEGORIES.includes(k as SiteFunctionCategory) && v.docs > 0) byCategory.push(v);
      }

      const byCountry = Array.from(countryMap.values())
        .filter((r) => r.docs > 0)
        .sort((a, b) => b.docs - a.docs);

      const bySheet = Array.from(sheetMap.values())
        .filter((r) => r.docs > 0)
        .sort((a, b) => b.docs - a.docs);

      const recentSites: DashboardRecentSite[] = perSite
        .filter((r) => !!r.last_crawled)
        .sort((a, b) => (b.last_crawled ?? "").localeCompare(a.last_crawled ?? ""))
        .slice(0, 8)
        .map((r) => {
          const cat = getCategoryForSheet(r.sheet);
          return {
            site_id: r.site_id,
            site_name: r.site_name,
            sheet: r.sheet,
            country: cat.country,
            continent: cat.continent,
            category: cat.category,
            docs: r.total,
            last_crawled: r.last_crawled,
          };
        });

      // classification coverage — based on registered sites, not docs.
      const registered = hasSites
        ? (db.prepare(`SELECT sheet FROM sites`).all() as { sheet: string | null }[])
        : perSite.map((r) => ({ sheet: r.sheet }));
      const cs = classificationStats(registered.map((r) => r.sheet));

      const kpi: DashboardKPI = {
        totalDocs: stats.totalPapers,
        totalSites: stats.totalSites,
        registeredSites: stats.registeredSites,
        pdfDocs: stats.pdfPapers,
        txtDocs: stats.txtPapers,
        summarizedDocs: perSite.reduce((acc, r) => acc + (r.summarized ?? 0), 0),
        lastCrawled: stats.lastCrawled,
      };

      return {
        kpi,
        byContinent,
        byCountry,
        byCategory,
        bySheet,
        recentSites,
        classification: {
          totalSheets: cs.total,
          mappedSheets: cs.mapped,
          unmappedSheets: cs.unmapped,
          unmappedList: cs.unmappedSheets,
        },
      };
    } finally {
      db.close();
    }
  });
}

// ====================================================================
// Site option enrichment — used by the search page filters so we can
// drive continent/country/category dropdowns without re-querying.
// ====================================================================

export interface SiteOptionRich {
  site_id: string;
  site_name: string;
  sheet: string | null;
  country: string;
  countryCode: string;
  continent: Continent;
  category: SiteFunctionCategory;
  docs: number;
}

export function getSiteOptionsRich(): SiteOptionRich[] {
  if (getDbState(["documents"]).kind !== "ready") return [];

  return cached("siteOptionsRich", TTL, () => {
    const db = getDb();
    try {
      const hasSites = hasTables(db, ["sites"]);
      const selectSheet = hasSites ? "s.sheet" : "NULL";
      const selectName = hasSites ? "COALESCE(s.site_name, d.site_id)" : "d.site_id";
      const joinClause = hasSites ? "LEFT JOIN sites s ON s.site_id = d.site_id" : "";

      const rows = db
        .prepare(
          `SELECT d.site_id          AS site_id,
                  ${selectName}       AS site_name,
                  ${selectSheet}      AS sheet,
                  COUNT(d.seq_id)     AS docs
           FROM documents d
           ${joinClause}
           GROUP BY d.site_id
           ORDER BY docs DESC, d.site_id ASC`,
        )
        .all() as { site_id: string; site_name: string; sheet: string | null; docs: number }[];

      return rows.map((r) => {
        const cat = getCategoryForSheet(r.sheet);
        return {
          site_id: r.site_id,
          site_name: r.site_name,
          sheet: r.sheet,
          country: cat.country,
          countryCode: cat.countryCode,
          continent: cat.continent,
          category: cat.category,
          docs: r.docs,
        };
      });
    } finally {
      db.close();
    }
  });
}

export function getAllSitesSummary(): SiteCount[] {
  if (getDbState(["documents"]).kind !== "ready") {
    return [];
  }

  return cached("sitesSummary", TTL, () => {
    const db = getDb();
    try {
      const rows = db
        .prepare(
          `SELECT d.site_id as site_id,
                  COALESCE(s.site_name, d.site_id) as site_name,
                  COUNT(d.seq_id) as count,
                  MAX(d.collected_at) as last_crawled
           FROM documents d
           LEFT JOIN sites s ON s.site_id = d.site_id
           GROUP BY d.site_id, COALESCE(s.site_name, d.site_id)
           ORDER BY count DESC, site_id ASC`
        )
        .all() as SiteCount[];
      return rows;
    } finally {
      db.close();
    }
  });
}

export function getSiteOptions(): SiteOption[] {
  return getAllSitesSummary().map((site) => ({
    site_id: site.site_id,
    site_name: site.site_name,
  }));
}

export function getRecentPapers(siteId?: string, limit = 20): Paper[] {
  if (getDbState(["documents"]).kind !== "ready") {
    return [];
  }

  const safeLimit = Math.max(1, Math.min(limit, 200));
  const key = `recentPapers:${siteId ?? "__all__"}:${safeLimit}`;
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      const rows = siteId
        ? (db
            .prepare(
              `SELECT ${DOC_COLS_FULL}
               FROM documents
               WHERE site_id = ?
               ORDER BY collected_at DESC, published_date DESC
               LIMIT ?`
            )
            .all(siteId, safeLimit) as DocRow[])
        : (db
            .prepare(
              `SELECT ${DOC_COLS_FULL}
               FROM documents
               ORDER BY collected_at DESC, published_date DESC
               LIMIT ?`
            )
            .all(safeLimit) as DocRow[]);
      return rows.map(documentToPaperShape);
    } finally {
      db.close();
    }
  });
}

function escapeMarkdown(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/([*_`#[\]])/g, "\\$1");
}

export function renderPapersMarkdown(papers: Paper[], title: string): string {
  const lines: string[] = [
    `# ${escapeMarkdown(title)}`,
    "",
    `- Exported at: ${new Date().toISOString()}`,
    `- Document count: ${papers.length}`,
    "",
  ];

  if (papers.length === 0) {
    lines.push("수집된 문서가 없습니다.", "");
    return lines.join("\n");
  }

  papers.forEach((paper, index) => {
    lines.push(`## ${index + 1}. ${escapeMarkdown(paper.title || "(제목 없음)")}`);
    lines.push("");
    lines.push(`- ID: \`${paper.id}\``);
    lines.push(`- Site: \`${paper.site_id}\``);
    if (paper.external_id) {
      lines.push(`- Post number: ${escapeMarkdown(paper.external_id)}`);
    }
    if (paper.published_date) {
      lines.push(`- Published: ${paper.published_date}`);
    }
    lines.push(`- Crawled: ${paper.crawled_at}`);
    if (paper.url) {
      lines.push(`- URL: ${paper.url}`);
    }
    if (paper.pdf_url) {
      lines.push(`- Attachment: ${paper.pdf_url}`);
    }
    if (paper.authors) {
      lines.push(`- Authors: ${escapeMarkdown(paper.authors)}`);
    }
    if (paper.department) {
      lines.push(`- Publisher: ${escapeMarkdown(paper.department)}`);
    }
    if (paper.keywords) {
      lines.push(`- Keywords: ${escapeMarkdown(paper.keywords)}`);
    }
    lines.push("");
    if (paper.abstract) {
      lines.push(escapeMarkdown(paper.abstract));
      lines.push("");
    }
  });

  return lines.join("\n");
}

// ====================================================================
// libertree: documents 테이블 (글로벌 INTEGER PK + 12자리 파일 매핑)
// docs/libertree.md 와 crawler/db_libertree.py 의 documents 스키마와
// 1:1 대응한다.
// ====================================================================

export interface LibertreeDocument {
  seq_id: number;
  collected_at: string;
  site_id: string;
  post_number: string | null;
  meta_url: string | null;
  title: string | null;
  published_date: string | null;
  listed_date: string | null;
  authors: string | null;       // ";" separated
  publisher: string | null;     // ";" separated
  journal: string | null;
  pdf_url: string | null;
  keywords: string | null;      // "," separated
  abstract: string | null;
  original_filename: string | null;
  pdf_downloaded: number | null;
  text_extracted: number | null;
  pdf_size_bytes: number | null;
  pdf_sha256: string | null;
  summary: string | null;
  summary_model: string | null;
  summary_at: string | null;
  // back-compat alias for components that still reference `.id`
  id: number;
}

const DOCUMENT_COLUMNS = `
  seq_id, collected_at, site_id, post_number, meta_url, title,
  published_date, listed_date, authors, publisher, journal,
  pdf_url, keywords, abstract, original_filename,
  pdf_downloaded, text_extracted, pdf_size_bytes, pdf_sha256,
  summary, summary_model, summary_at
`;

function toLibertreeDocument(row: DocRow): LibertreeDocument {
  return {
    seq_id: row.seq_id,
    collected_at: row.collected_at,
    site_id: row.site_id,
    post_number: row.post_number,
    meta_url: row.meta_url,
    title: row.title,
    published_date: row.published_date,
    listed_date: row.listed_date,
    authors: row.authors,
    publisher: row.publisher,
    journal: row.journal,
    pdf_url: row.pdf_url,
    keywords: row.keywords,
    abstract: row.abstract,
    original_filename: row.original_filename,
    pdf_downloaded: row.pdf_downloaded,
    text_extracted: row.text_extracted,
    pdf_size_bytes: row.pdf_size_bytes,
    pdf_sha256: row.pdf_sha256,
    summary: row.summary,
    summary_model: row.summary_model,
    summary_at: row.summary_at,
    id: row.seq_id,
  };
}

export function getRecentDocuments(limit = 50): LibertreeDocument[] {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return [];
    const rows = db
      .prepare(
        `SELECT ${DOCUMENT_COLUMNS} FROM documents
         ORDER BY seq_id DESC
         LIMIT ?`
      )
      .all(limit) as DocRow[];
    return rows.map(toLibertreeDocument);
  } finally {
    db.close();
  }
}

export function getDocumentById(id: number): LibertreeDocument | null {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return null;
    const row = db
      .prepare(`SELECT ${DOCUMENT_COLUMNS} FROM documents WHERE seq_id = ?`)
      .get(id) as DocRow | undefined;
    return row ? toLibertreeDocument(row) : null;
  } finally {
    db.close();
  }
}

// ====================================================================
// Document detail (UI-friendly) — JOIN sites + check blob existence.
// Used by /search/[id] and /admin/document/[seq_id].
// ====================================================================

export interface DocumentDetail extends LibertreeDocument {
  site_name: string | null;
  site_url: string | null;
  blob_pdf_path: string;
  blob_txt_path: string;
  blob_pdf_abs: string;
  blob_txt_abs: string;
  blob_pdf_exists: boolean;
  blob_txt_exists: boolean;
  blob_pdf_disk_size: number | null;
  blob_txt_disk_size: number | null;
}

interface DocDetailRow extends DocRow {
  site_name: string | null;
  site_url: string | null;
}

export function getDocumentDetail(seqId: number): DocumentDetail | null {
  if (!Number.isInteger(seqId) || seqId < 0 || seqId >= 1e12) return null;
  if (getDbState(["documents"]).kind !== "ready") return null;

  const db = getDb();
  let row: DocDetailRow | undefined;
  try {
    const hasSites = hasTables(db, ["sites"]);
    const select = hasSites
      ? `SELECT ${DOCUMENT_COLUMNS}, s.site_name AS site_name, s.site_url AS site_url
         FROM documents d
         LEFT JOIN sites s ON s.site_id = d.site_id
         WHERE d.seq_id = ?`
      : `SELECT ${DOCUMENT_COLUMNS}, NULL AS site_name, NULL AS site_url
         FROM documents WHERE seq_id = ?`;
    // For the JOIN form we need column references against `d.*` — re-issue the SELECT
    // using qualified column list when sites table exists.
    if (hasSites) {
      const qualified = `
        d.seq_id, d.collected_at, d.site_id, d.post_number, d.meta_url, d.title,
        d.published_date, d.listed_date, d.authors, d.publisher, d.journal,
        d.pdf_url, d.keywords, d.abstract, d.original_filename,
        d.pdf_downloaded, d.text_extracted, d.pdf_size_bytes, d.pdf_sha256,
        d.summary, d.summary_model, d.summary_at,
        s.site_name AS site_name, s.site_url AS site_url
      `;
      row = db
        .prepare(
          `SELECT ${qualified}
           FROM documents d
           LEFT JOIN sites s ON s.site_id = d.site_id
           WHERE d.seq_id = ?`
        )
        .get(seqId) as DocDetailRow | undefined;
    } else {
      row = db.prepare(select).get(seqId) as DocDetailRow | undefined;
    }
  } finally {
    db.close();
  }

  if (!row) return null;

  const pdfPath = blobPath(seqId, "pdf");
  const txtPath = blobPath(seqId, "txt");
  // libertree/ lives at project root (one level up from finolaw/).
  const projectRoot = path.join(process.cwd(), "..");
  const pdfAbs = path.join(projectRoot, pdfPath);
  const txtAbs = path.join(projectRoot, txtPath);

  let pdfExists = false;
  let txtExists = false;
  let pdfDiskSize: number | null = null;
  let txtDiskSize: number | null = null;
  try {
    const st = fs.statSync(pdfAbs);
    pdfExists = st.isFile();
    pdfDiskSize = pdfExists ? st.size : null;
  } catch {
    /* missing */
  }
  try {
    const st = fs.statSync(txtAbs);
    txtExists = st.isFile();
    txtDiskSize = txtExists ? st.size : null;
  } catch {
    /* missing */
  }

  const base = toLibertreeDocument(row);
  return {
    ...base,
    site_name: row.site_name ?? null,
    site_url: row.site_url ?? null,
    blob_pdf_path: pdfPath,
    blob_txt_path: txtPath,
    blob_pdf_abs: pdfAbs,
    blob_txt_abs: txtAbs,
    blob_pdf_exists: pdfExists,
    blob_txt_exists: txtExists,
    blob_pdf_disk_size: pdfDiskSize,
    blob_txt_disk_size: txtDiskSize,
  };
}

/**
 * Resolve the absolute filesystem path for a blob, validating seq_id.
 * Throws RangeError for out-of-range ids. Used by the blob streaming route.
 */
export function blobAbsolutePath(seqId: number, ext: "pdf" | "txt"): string {
  return path.join(process.cwd(), "..", blobPath(seqId, ext));
}

/** Look up the original_filename + site_id for the blob download header. */
export function getDocumentBlobInfo(
  seqId: number
): { original_filename: string | null; site_id: string } | null {
  if (!Number.isInteger(seqId) || seqId < 0 || seqId >= 1e12) return null;
  if (getDbState(["documents"]).kind !== "ready") return null;
  const db = getDb();
  try {
    const row = db
      .prepare(
        `SELECT original_filename, site_id FROM documents WHERE seq_id = ?`
      )
      .get(seqId) as
      | { original_filename: string | null; site_id: string }
      | undefined;
    return row ?? null;
  } finally {
    db.close();
  }
}

export interface SearchDocumentsParams {
  query?: string;        // matched against title/abstract/authors/keywords (LIKE)
  siteId?: string;
  limit?: number;
  offset?: number;
}

export function searchDocuments({
  query,
  siteId,
  limit = 50,
  offset = 0,
}: SearchDocumentsParams = {}): LibertreeDocument[] {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return [];
    const wheres: string[] = [];
    const params: (string | number)[] = [];
    if (siteId) {
      wheres.push("site_id = ?");
      params.push(siteId);
    }
    if (query && query.trim().length > 0) {
      const q = `%${query.trim()}%`;
      wheres.push("(title LIKE ? OR abstract LIKE ? OR authors LIKE ? OR keywords LIKE ?)");
      params.push(q, q, q, q);
    }
    const where = wheres.length ? `WHERE ${wheres.join(" AND ")}` : "";
    params.push(limit, offset);
    const rows = db
      .prepare(
        `SELECT ${DOCUMENT_COLUMNS} FROM documents
         ${where}
         ORDER BY seq_id DESC
         LIMIT ? OFFSET ?`
      )
      .all(...params) as DocRow[];
    return rows.map(toLibertreeDocument);
  } finally {
    db.close();
  }
}

export function countDocuments(siteId?: string): number {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return 0;
    if (siteId) {
      const row = db
        .prepare("SELECT COUNT(*) AS n FROM documents WHERE site_id = ?")
        .get(siteId) as { n: number };
      return row.n;
    }
    const row = db
      .prepare("SELECT COUNT(*) AS n FROM documents")
      .get() as { n: number };
    return row.n;
  } finally {
    db.close();
  }
}

export function splitAuthors(authors: string | null): string[] {
  if (!authors) return [];
  return authors
    .split(";")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export function splitKeywords(keywords: string | null): string[] {
  if (!keywords) return [];
  return keywords
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export function splitPublishers(publisher: string | null): string[] {
  return splitAuthors(publisher);
}

// ====================================================================
// /admin/status — per-site collection progress
// libertree uses INTEGER flag columns (pdf_downloaded, text_extracted)
// instead of the old papers.db `download_status` / `txt_path` columns.
// ====================================================================

export interface SiteProgress {
  site_id: string;
  site_name: string;
  total: number;
  downloaded: number;
  converted: number;
  summarized: number;
  last_crawled: string | null;
}

export interface CollectionTotals {
  total: number;
  downloaded: number;
  converted: number;
  summarized: number;
  sites: number;
}

export function getCollectionProgress(): SiteProgress[] {
  if (getDbState(["documents"]).kind !== "ready") return [];

  return cached("collectionProgress", TTL, () => {
    const db = getDb();
    try {
      const rows = db
        .prepare(
          `SELECT d.site_id AS site_id,
                  COALESCE(s.site_name, d.site_id) AS site_name,
                  COUNT(d.seq_id) AS total,
                  SUM(CASE WHEN d.pdf_downloaded = 1 THEN 1 ELSE 0 END) AS downloaded,
                  SUM(CASE WHEN d.text_extracted = 1 THEN 1 ELSE 0 END) AS converted,
                  SUM(CASE WHEN d.summary IS NOT NULL AND d.summary != '' THEN 1 ELSE 0 END) AS summarized,
                  MAX(d.collected_at) AS last_crawled
           FROM documents d
           LEFT JOIN sites s ON s.site_id = d.site_id
           GROUP BY d.site_id, COALESCE(s.site_name, d.site_id)
           ORDER BY total DESC, site_id ASC`
        )
        .all() as SiteProgress[];
      return rows;
    } finally {
      db.close();
    }
  });
}

export interface SummaryFilter {
  hasSummary?: boolean;
  siteId?: string;
  limit?: number;
  offset?: number;
}

export interface SummaryListResult {
  documents: LibertreeDocument[];
  total: number;
  limit: number;
  offset: number;
}

export function getDocumentsBySummaryStatus({
  hasSummary,
  siteId,
  limit = 50,
  offset = 0,
}: SummaryFilter = {}): SummaryListResult {
  if (getDbState(["documents"]).kind !== "ready") {
    return { documents: [], total: 0, limit, offset };
  }

  const db = getDb();
  try {
    const wheres: string[] = [];
    const params: (string | number)[] = [];
    if (typeof hasSummary === "boolean") {
      wheres.push(
        hasSummary
          ? "(summary IS NOT NULL AND summary != '')"
          : "(summary IS NULL OR summary = '')",
      );
    }
    if (siteId) {
      wheres.push("site_id = ?");
      params.push(siteId);
    }
    const where = wheres.length ? `WHERE ${wheres.join(" AND ")}` : "";

    const total = (
      db.prepare(`SELECT COUNT(*) AS c FROM documents ${where}`)
        .get(...params) as { c: number }
    ).c;

    const rows = db
      .prepare(
        `SELECT ${DOCUMENT_COLUMNS} FROM documents
         ${where}
         ORDER BY seq_id DESC
         LIMIT ? OFFSET ?`,
      )
      .all(...params, limit, offset) as DocRow[];

    return {
      documents: rows.map(toLibertreeDocument),
      total,
      limit,
      offset,
    };
  } finally {
    db.close();
  }
}

export function getCollectionTotals(): CollectionTotals {
  const rows = getCollectionProgress();
  return rows.reduce<CollectionTotals>(
    (acc, r) => ({
      total: acc.total + (r.total | 0),
      downloaded: acc.downloaded + (r.downloaded | 0),
      converted: acc.converted + (r.converted | 0),
      summarized: acc.summarized + (r.summarized | 0),
      sites: acc.sites + 1,
    }),
    { total: 0, downloaded: 0, converted: 0, summarized: 0, sites: 0 },
  );
}

// ===========================================================================
// Collection report (input -> registered -> collected funnel + gap analysis)
// ===========================================================================

export interface SiteGap {
  site_id: string;
  total: number;
  pdf_downloaded: number;
  pdf_failed: number;
  no_pdf_url: number;
}

export interface RecoveryCategory {
  category: string;
  count: number;
}

export interface CollectionReport {
  inputEntries: number;
  inputUniqueHosts: number;
  registeredCrawlers: number;
  collectedSites: number;
  totalDocs: number;
  totalPdfDownloaded: number;
  totalTextExtracted: number;
  totalSummary: number;
  totalPdfFailed: number;
  totalNoPdfUrl: number;
  siteGaps: SiteGap[];
  recoveryCategories: RecoveryCategory[];
}

function countCsvRows(absPath: string): number {
  try {
    if (!fs.existsSync(absPath)) return 0;
    const data = fs.readFileSync(absPath, "utf-8");
    const lines = data.split(/\r?\n/).filter((l) => l.trim().length > 0);
    return Math.max(0, lines.length - 1); // header
  } catch {
    return 0;
  }
}

function countCsvUniqueHosts(absPath: string, hostCol: string): number {
  try {
    if (!fs.existsSync(absPath)) return 0;
    const data = fs.readFileSync(absPath, "utf-8");
    const lines = data.split(/\r?\n/);
    if (lines.length < 2) return 0;
    // Strip UTF-8 BOM if present
    const headerLine = lines[0].replace(/^﻿/, "");
    const header = headerLine.split(",");
    const idx = header.indexOf(hostCol);
    if (idx < 0) return 0;
    const set = new Set<string>();
    for (let i = 1; i < lines.length; i++) {
      const ln = lines[i];
      if (!ln) continue;
      const cols = ln.split(",");
      const v = (cols[idx] || "").trim();
      if (v) set.add(v);
    }
    return set.size;
  } catch {
    return 0;
  }
}

function readCsvCategoryCounts(absPath: string, catCol: string): RecoveryCategory[] {
  try {
    if (!fs.existsSync(absPath)) return [];
    const data = fs.readFileSync(absPath, "utf-8");
    const lines = data.split(/\r?\n/);
    if (lines.length < 2) return [];
    const headerLine = lines[0].replace(/^﻿/, "");
    const header = headerLine.split(",");
    const idx = header.indexOf(catCol);
    if (idx < 0) return [];
    const counts = new Map<string, number>();
    for (let i = 1; i < lines.length; i++) {
      const ln = lines[i];
      if (!ln) continue;
      const cols = ln.split(",");
      const v = (cols[idx] || "").trim();
      if (!v) continue;
      counts.set(v, (counts.get(v) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(([category, count]) => ({ category, count }))
      .sort((a, b) => b.count - a.count);
  } catch {
    return [];
  }
}

function countRegisteredCrawlers(): number {
  try {
    const projectRoot = path.resolve(process.cwd(), "..");
    const customDir = path.join(projectRoot, "crawler", "sites", "custom");
    const configsDir = path.join(projectRoot, "crawler", "sites", "configs");
    let n = 3; // ntrs, mohw, fsc built-in
    if (fs.existsSync(customDir)) {
      const files = fs.readdirSync(customDir);
      n += files.filter((f) => f.endsWith(".py") && !f.startsWith("_")).length;
    }
    if (fs.existsSync(configsDir)) {
      const files = fs.readdirSync(configsDir);
      n += files.filter((f) => f.endsWith(".json")).length;
    }
    return n;
  } catch {
    return 0;
  }
}

export interface SiteGapDiagnosis {
  gap_count: number;
  samples: number;
  dominant: string;
  reasons: Record<string, number>;
}

export function getPdfGapDiagnosis(): Record<string, SiteGapDiagnosis> {
  try {
    const p = path.resolve(process.cwd(), "..", "data", "audit", "pdf_gap_diagnosis.json");
    if (!fs.existsSync(p)) return {};
    const raw = JSON.parse(fs.readFileSync(p, "utf-8")) as {
      sites?: Record<string, SiteGapDiagnosis>;
    };
    return raw.sites || {};
  } catch {
    return {};
  }
}

export const PDF_GAP_REASON_LABEL: Record<string, string> = {
  retry_ok: "🟢 일시오류 (재시도 가능)",
  embargo_html: "🟡 HTML landing (embargo 가능성)",
  permanent_404: "🔴 영구 404",
  client_error: "🔴 4xx 오류",
  server_error: "🟠 5xx 오류",
  timeout: "🟠 timeout",
  conn_error: "🟠 연결 실패",
  ssl_error: "🟠 SSL 오류",
  no_url: "📄 pdf_url 없음",
  other: "❓ 기타",
};

export function getCollectionReport(): CollectionReport {
  if (getDbState(["documents"]).kind !== "ready") {
    return {
      inputEntries: 0,
      inputUniqueHosts: 0,
      registeredCrawlers: 0,
      collectedSites: 0,
      totalDocs: 0,
      totalPdfDownloaded: 0,
      totalTextExtracted: 0,
      totalSummary: 0,
      totalPdfFailed: 0,
      totalNoPdfUrl: 0,
      siteGaps: [],
      recoveryCategories: [],
    };
  }

  return cached("collectionReport", TTL, () => {
    const projectRoot = path.resolve(process.cwd(), "..");
    const coverageCsv = path.join(projectRoot, "data", "audit", "coverage_report.csv");
    const recoveryCsv = path.join(projectRoot, "data", "audit", "site_recovery_plan.csv");

    const inputEntries = countCsvRows(coverageCsv);
    const inputUniqueHosts = countCsvUniqueHosts(coverageCsv, "host");
    const recoveryCategories = readCsvCategoryCounts(recoveryCsv, "recovery_category");
    const registeredCrawlers = countRegisteredCrawlers();

    const db = getDb();
    try {
      const totals = db
        .prepare(
          `SELECT COUNT(*) AS docs,
                  SUM(CASE WHEN pdf_downloaded=1 THEN 1 ELSE 0 END) AS pdfs,
                  SUM(CASE WHEN text_extracted=1 THEN 1 ELSE 0 END) AS txts,
                  SUM(CASE WHEN COALESCE(summary,'')!='' THEN 1 ELSE 0 END) AS sums,
                  SUM(CASE WHEN COALESCE(pdf_url,'')!='' AND pdf_downloaded=0 THEN 1 ELSE 0 END) AS pdf_fail,
                  SUM(CASE WHEN COALESCE(pdf_url,'')='' THEN 1 ELSE 0 END) AS no_pdf_url,
                  COUNT(DISTINCT site_id) AS sites
           FROM documents`
        )
        .get() as {
          docs: number; pdfs: number; txts: number; sums: number;
          pdf_fail: number; no_pdf_url: number; sites: number;
        };

      const siteGaps = db
        .prepare(
          `SELECT site_id,
                  COUNT(*) AS total,
                  SUM(CASE WHEN pdf_downloaded=1 THEN 1 ELSE 0 END) AS pdf_downloaded,
                  SUM(CASE WHEN COALESCE(pdf_url,'')!='' AND pdf_downloaded=0 THEN 1 ELSE 0 END) AS pdf_failed,
                  SUM(CASE WHEN COALESCE(pdf_url,'')='' THEN 1 ELSE 0 END) AS no_pdf_url
           FROM documents
           GROUP BY site_id
           ORDER BY total DESC`
        )
        .all() as SiteGap[];

      return {
        inputEntries,
        inputUniqueHosts,
        registeredCrawlers,
        collectedSites: totals.sites | 0,
        totalDocs: totals.docs | 0,
        totalPdfDownloaded: totals.pdfs | 0,
        totalTextExtracted: totals.txts | 0,
        totalSummary: totals.sums | 0,
        totalPdfFailed: totals.pdf_fail | 0,
        totalNoPdfUrl: totals.no_pdf_url | 0,
        siteGaps,
        recoveryCategories,
      };
    } finally {
      db.close();
    }
  });
}

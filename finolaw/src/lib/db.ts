import Database from "better-sqlite3";
import path from "path";
import { cached } from "./cache";

const DB_PATH = path.join(process.cwd(), "..", "data", "papers.db");

export const NTS_SITES = ["nts-taxlaw-pd", "nts-taxlaw-qt"] as const;
export type NtsSiteId = (typeof NTS_SITES)[number];

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
}

function getDb() {
  return new Database(DB_PATH, { readonly: true, fileMustExist: true });
}

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

export function searchPapers(filters: SearchFilters): SearchResult {
  const db = getDb();
  try {
    const {
      siteIds = [...NTS_SITES],
      q,
      category,
      docType,
      dateFrom,
      dateTo,
      page = 1,
      pageSize = 20,
    } = filters;

    const offset = (page - 1) * pageSize;
    const ftsQuery = q ? sanitizeFtsQuery(q) : null;

    if (ftsQuery) {
      // ── FTS5 path ──────────────────────────────────────────────────────────
      const where: string[] = [];
      const params: unknown[] = [ftsQuery];

      if (siteIds.length > 0) {
        where.push(`p.site_id IN (${siteIds.map(() => "?").join(",")})`);
        params.push(...siteIds);
      }
      if (category) {
        where.push(`p.category = ?`);
        params.push(category);
      }
      if (docType) {
        where.push(`json_extract(p.metadata, '$.documentTypeName') = ?`);
        params.push(docType);
      }
      if (dateFrom) {
        where.push(`p.published_date >= ?`);
        params.push(dateFrom);
      }
      if (dateTo) {
        where.push(`p.published_date <= ?`);
        params.push(dateTo);
      }

      const whereSQL = where.length ? `AND ${where.join(" AND ")}` : "";

      const total = (
        db
          .prepare(
            `SELECT COUNT(*) as c
             FROM papers p
             JOIN papers_fts f ON p.rowid = f.rowid
             WHERE papers_fts MATCH ?
             ${whereSQL}`
          )
          .get(...params) as { c: number }
      ).c;

      const rows = db
        .prepare(
          `SELECT p.* FROM papers p
           JOIN papers_fts f ON p.rowid = f.rowid
           WHERE papers_fts MATCH ?
           ${whereSQL}
           ORDER BY p.published_date DESC, p.crawled_at DESC
           LIMIT ? OFFSET ?`
        )
        .all(...params, pageSize, offset) as Paper[];

      return { papers: rows, total, page, pageSize };
    }

    // ── Non-FTS path (no q, or q had no usable tokens) ─────────────────────
    const where: string[] = [];
    const params: unknown[] = [];

    if (siteIds.length > 0) {
      where.push(`site_id IN (${siteIds.map(() => "?").join(",")})`);
      params.push(...siteIds);
    }
    if (category) {
      where.push(`category = ?`);
      params.push(category);
    }
    if (docType) {
      where.push(`json_extract(metadata, '$.documentTypeName') = ?`);
      params.push(docType);
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
        .prepare(`SELECT COUNT(*) as c FROM papers ${whereSQL}`)
        .get(...params) as { c: number }
    ).c;

    const rows = db
      .prepare(
        `SELECT * FROM papers ${whereSQL} ORDER BY published_date DESC, crawled_at DESC LIMIT ? OFFSET ?`
      )
      .all(...params, pageSize, offset) as Paper[];

    return { papers: rows, total, page, pageSize };
  } finally {
    db.close();
  }
}

export function getPaper(id: string): Paper | null {
  const db = getDb();
  try {
    return (db.prepare(`SELECT * FROM papers WHERE id = ?`).get(id) as Paper) ?? null;
  } finally {
    db.close();
  }
}

export function getDocTypeCounts(siteIds: string[] = [...NTS_SITES]): DocTypeCount[] {
  const key = "docTypeCounts:" + [...siteIds].sort().join(",");
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      const placeholders = siteIds.map(() => "?").join(",");
      const rows = db
        .prepare(
          `SELECT json_extract(metadata, '$.documentTypeName') AS documentTypeName,
                  COUNT(*) AS count
           FROM papers
           WHERE site_id IN (${placeholders})
           GROUP BY documentTypeName
           ORDER BY count DESC`
        )
        .all(...siteIds) as DocTypeCount[];
      return rows.filter((r) => r.documentTypeName);
    } finally {
      db.close();
    }
  });
}

export function getCategories(siteIds: string[] = [...NTS_SITES]): string[] {
  const key = "categories:" + [...siteIds].sort().join(",");
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      const placeholders = siteIds.map(() => "?").join(",");
      const rows = db
        .prepare(
          `SELECT DISTINCT category FROM papers
           WHERE site_id IN (${placeholders}) AND category IS NOT NULL AND category != ''
           ORDER BY category`
        )
        .all(...siteIds) as { category: string }[];
      return rows.map((r) => r.category);
    } finally {
      db.close();
    }
  });
}

export interface DbStats {
  totalPapers: number;
  ntsPd: number;
  ntsQt: number;
  totalSites: number;
  lastCrawled: string | null;
}

export function getDbStats(): DbStats {
  return cached("stats", TTL, () => {
    const db = getDb();
    try {
      const total = (db.prepare(`SELECT COUNT(*) as c FROM papers`).get() as {
        c: number;
      }).c;
      const ntsPd = (db
        .prepare(`SELECT COUNT(*) as c FROM papers WHERE site_id = 'nts-taxlaw-pd'`)
        .get() as { c: number }).c;
      const ntsQt = (db
        .prepare(`SELECT COUNT(*) as c FROM papers WHERE site_id = 'nts-taxlaw-qt'`)
        .get() as { c: number }).c;
      const sites = (db.prepare(`SELECT COUNT(*) as c FROM sites`).get() as {
        c: number;
      }).c;
      const last = db
        .prepare(
          `SELECT MAX(crawled_at) as last FROM papers WHERE site_id LIKE 'nts-taxlaw%'`
        )
        .get() as { last: string | null };
      return {
        totalPapers: total,
        ntsPd,
        ntsQt,
        totalSites: sites,
        lastCrawled: last.last,
      };
    } finally {
      db.close();
    }
  });
}

export function getAllSitesSummary(): SiteCount[] {
  return cached("sitesSummary", TTL, () => {
    const db = getDb();
    try {
      const rows = db
        .prepare(
          `SELECT s.id as site_id, s.name as site_name, COUNT(p.id) as count
           FROM sites s
           LEFT JOIN papers p ON p.site_id = s.id
           GROUP BY s.id, s.name
           HAVING count > 0
           ORDER BY count DESC`
        )
        .all() as SiteCount[];
      return rows;
    } finally {
      db.close();
    }
  });
}

import fs from "fs";
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
       WHERE type = 'table' AND name IN (${placeholders})`
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
       FROM papers
       WHERE site_id IS NOT NULL AND site_id != ''
       ORDER BY site_id`
    )
    .all() as { site_id: string }[];
  return rows.map((row) => row.site_id);
}

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
  if (getDbState(["papers"]).kind !== "ready") {
    return emptySearchResult(filters);
  }

  const db = getDb();
  try {
    const resolvedSiteIds = normalizeSiteIds(db, filters.siteIds);
    const {
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

    if (ftsQuery && hasTables(db, ["papers_fts"])) {
      // ── FTS5 path ──────────────────────────────────────────────────────────
      const where: string[] = [];
      const params: unknown[] = [ftsQuery];

      if (resolvedSiteIds.length > 0) {
        where.push(`p.site_id IN (${resolvedSiteIds.map(() => "?").join(",")})`);
        params.push(...resolvedSiteIds);
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

    // ── Non-FTS path (no q, q had no usable tokens, or FTS is unavailable) ──
    const where: string[] = [];
    const params: unknown[] = [];
    const tokens = q?.trim().split(/\s+/).filter((token) => token.length > 0) ?? [];

    if (tokens.length > 0) {
      tokens.forEach((token) => {
        const pattern = `%${token}%`;
        where.push(
          `(COALESCE(title, '') LIKE ? OR COALESCE(abstract, '') LIKE ? OR COALESCE(metadata, '') LIKE ?)`
        );
        params.push(pattern, pattern, pattern);
      });
    }

    if (resolvedSiteIds.length > 0) {
      where.push(`site_id IN (${resolvedSiteIds.map(() => "?").join(",")})`);
      params.push(...resolvedSiteIds);
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
  if (getDbState(["papers"]).kind !== "ready") {
    return null;
  }

  const db = getDb();
  try {
    return (db.prepare(`SELECT * FROM papers WHERE id = ?`).get(id) as Paper) ?? null;
  } finally {
    db.close();
  }
}

export function getDocTypeCounts(siteIds?: string[]): DocTypeCount[] {
  if (getDbState(["papers"]).kind !== "ready") {
    return [];
  }

  const cacheSiteIds = siteIds && siteIds.length > 0 ? [...siteIds].sort() : ["__all__"];
  const key = "docTypeCounts:" + cacheSiteIds.join(",");
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      const resolvedSiteIds = normalizeSiteIds(db, siteIds);
      const rows =
        resolvedSiteIds.length > 0
          ? (db
              .prepare(
                `SELECT json_extract(metadata, '$.documentTypeName') AS documentTypeName,
                        COUNT(*) AS count
                 FROM papers
                 WHERE site_id IN (${resolvedSiteIds.map(() => "?").join(",")})
                 GROUP BY documentTypeName
                 ORDER BY count DESC`
              )
              .all(...resolvedSiteIds) as DocTypeCount[])
          : [];
      return rows.filter((r) => r.documentTypeName);
    } finally {
      db.close();
    }
  });
}

export function getCategories(siteIds?: string[]): string[] {
  if (getDbState(["papers"]).kind !== "ready") {
    return [];
  }

  const cacheSiteIds = siteIds && siteIds.length > 0 ? [...siteIds].sort() : ["__all__"];
  const key = "categories:" + cacheSiteIds.join(",");
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      const resolvedSiteIds = normalizeSiteIds(db, siteIds);
      const rows =
        resolvedSiteIds.length > 0
          ? (db
              .prepare(
                `SELECT DISTINCT category FROM papers
                 WHERE site_id IN (${resolvedSiteIds.map(() => "?").join(",")})
                   AND category IS NOT NULL
                   AND category != ''
                 ORDER BY category`
              )
              .all(...resolvedSiteIds) as { category: string }[])
          : [];
      return rows.map((r) => r.category);
    } finally {
      db.close();
    }
  });
}

export interface DbStats {
  totalPapers: number;
  customPapers: number;
  totalSites: number;
  registeredSites: number;
  lastCrawled: string | null;
}

export function getDbStats(): DbStats {
  if (getDbState(["papers"]).kind !== "ready") {
    return {
      totalPapers: 0,
      customPapers: 0,
      totalSites: 0,
      registeredSites: 0,
      lastCrawled: null,
    };
  }

  return cached("stats", TTL, () => {
    const db = getDb();
    try {
      const total = (db.prepare(`SELECT COUNT(*) as c FROM papers`).get() as {
        c: number;
      }).c;
      const customPapers = (db
        .prepare(
          `SELECT COUNT(*) as c
           FROM papers
           WHERE site_id NOT IN ('nts-taxlaw-pd', 'nts-taxlaw-qt')`
        )
        .get() as { c: number }).c;
      const totalSites = (db
        .prepare(`SELECT COUNT(DISTINCT site_id) as c FROM papers`)
        .get() as { c: number }).c;
      const registeredSites = hasTables(db, ["sites"])
        ? (db.prepare(`SELECT COUNT(*) as c FROM sites`).get() as {
            c: number;
          }).c
        : 0;
      const last = db.prepare(`SELECT MAX(crawled_at) as last FROM papers`).get() as {
        last: string | null;
      };
      return {
        totalPapers: total,
        customPapers,
        totalSites,
        registeredSites,
        lastCrawled: last.last,
      };
    } finally {
      db.close();
    }
  });
}

export function getAllSitesSummary(): SiteCount[] {
  if (getDbState(["papers"]).kind !== "ready") {
    return [];
  }

  return cached("sitesSummary", TTL, () => {
    const db = getDb();
    try {
      const rows = db
        .prepare(
          `SELECT p.site_id as site_id,
                  COALESCE(s.name, p.site_id) as site_name,
                  COUNT(p.id) as count,
                  MAX(p.crawled_at) as last_crawled
           FROM papers p
           LEFT JOIN sites s ON p.site_id = s.id
           GROUP BY p.site_id, COALESCE(s.name, p.site_id)
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
  if (getDbState(["papers"]).kind !== "ready") {
    return [];
  }

  const safeLimit = Math.max(1, Math.min(limit, 200));
  const key = `recentPapers:${siteId ?? "__all__"}:${safeLimit}`;
  return cached(key, TTL, () => {
    const db = getDb();
    try {
      if (siteId) {
        return db
          .prepare(
            `SELECT *
             FROM papers
             WHERE site_id = ?
             ORDER BY crawled_at DESC, published_date DESC
             LIMIT ?`
          )
          .all(siteId, safeLimit) as Paper[];
      }
      return db
        .prepare(
          `SELECT *
           FROM papers
           ORDER BY crawled_at DESC, published_date DESC
           LIMIT ?`
        )
        .all(safeLimit) as Paper[];
    } finally {
      db.close();
    }
  });
}

function escapeMarkdown(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/([*_`#[\]])/g, "\\$1");
}

function toBulletList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (item && typeof item === "object" && "name" in item && typeof item.name === "string") {
        return item.name;
      }
      return null;
    })
    .filter((item): item is string => Boolean(item));
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
    const metadata = parseMetadata(paper.metadata);
    const relatedLaws = toBulletList(metadata.relatedLaws);
    const relatedTopics = toBulletList(metadata.relatedTopics);

    lines.push(`## ${index + 1}. ${escapeMarkdown(paper.title || "(제목 없음)")}`);
    lines.push("");
    lines.push(`- ID: \`${paper.id}\``);
    lines.push(`- Site: \`${paper.site_id}\``);
    if (metadata.documentTypeName) {
      lines.push(`- Type: ${escapeMarkdown(String(metadata.documentTypeName))}`);
    }
    if (metadata.documentNumber) {
      lines.push(`- Document Number: ${escapeMarkdown(String(metadata.documentNumber))}`);
    }
    if (paper.category) {
      lines.push(`- Category: ${escapeMarkdown(paper.category)}`);
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
    if (relatedLaws.length > 0) {
      lines.push(`- Related Laws: ${relatedLaws.map((law) => escapeMarkdown(law)).join(", ")}`);
    }
    if (relatedTopics.length > 0) {
      lines.push(`- Related Topics: ${relatedTopics.map((topic) => escapeMarkdown(topic)).join(", ")}`);
    }
    lines.push("");
    if (paper.abstract) {
      lines.push(escapeMarkdown(paper.abstract));
      lines.push("");
    }
  });

  return lines.join("\n");
}

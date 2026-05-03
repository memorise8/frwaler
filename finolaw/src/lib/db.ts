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
       FROM documents
       WHERE site_id IS NOT NULL AND site_id != ''
       ORDER BY site_id`
    )
    .all() as { site_id: string }[];
  return rows.map((row) => row.site_id);
}

// ====================================================================
// Paper-shape adapter over the documents table.
// All Paper-shape readers below run against `documents` and shape rows
// into the legacy `Paper` interface so the existing UI pages stay unchanged.
// `papers` is kept read-only as a fallback for legacy UUID lookups.
// ====================================================================

interface DocRow {
  id: number;
  crawled_at: string;
  site_id: string;
  external_id: string | null;
  meta_url: string | null;
  title: string | null;
  published_date: string | null;
  posted_date: string | null;
  authors: string | null;
  publisher: string | null;
  journal: string | null;
  pdf_url: string | null;
  keywords: string | null;
  abstract: string | null;
  original_filename: string | null;
  pdf_path: string | null;
  txt_path: string | null;
  download_status: string | null;
  summary: string | null;
  metadata: string | null;
}

function safeJSONParse(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function pickStr(md: Record<string, unknown>, key: string): string | null {
  const v = md[key];
  return typeof v === "string" && v ? v : null;
}

function documentToPaperShape(d: DocRow): Paper {
  const md = safeJSONParse(d.metadata);
  return {
    id: String(d.id),
    site_id: d.site_id,
    external_id: d.external_id ?? "",
    title: d.title,
    authors: d.authors,
    abstract: d.abstract,
    category: pickStr(md, "category"),
    keywords: d.keywords,
    published_date: d.published_date,
    url: d.meta_url,
    pdf_url: d.pdf_url,
    doi: pickStr(md, "doi"),
    department: d.publisher,
    metadata: d.metadata,
    crawled_at: d.crawled_at,
  };
}

const DOC_COLS_FULL = `
  id, crawled_at, site_id, external_id, meta_url, title,
  published_date, posted_date, authors, publisher, journal,
  pdf_url, keywords, abstract, original_filename,
  pdf_path, txt_path, download_status, summary, metadata
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
      category,
      docType,
      dateFrom,
      dateTo,
      page = 1,
      pageSize = 20,
    } = filters;

    const offset = (page - 1) * pageSize;

    // FTS5 path is temporarily disabled — `papers_fts` only indexes the
    // legacy `papers` table. A `documents_fts` rebuild is a follow-up PR.
    // Use LIKE path for all queries against `documents`.
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
      where.push(`json_extract(metadata, '$.category') = ?`);
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
        .prepare(`SELECT COUNT(*) as c FROM documents ${whereSQL}`)
        .get(...params) as { c: number }
    ).c;

    const rows = db
      .prepare(
        `SELECT ${DOC_COLS_FULL} FROM documents ${whereSQL}
         ORDER BY published_date DESC, crawled_at DESC LIMIT ? OFFSET ?`
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
    // Numeric IDs map to the new `documents` table; non-numeric (UUID) IDs
    // fall back to the legacy `papers` table for smart-find historical rows.
    const isNumeric = /^\d+$/.test(id);
    if (isNumeric && hasTables(db, ["documents"])) {
      const row = db
        .prepare(`SELECT ${DOC_COLS_FULL} FROM documents WHERE id = ?`)
        .get(Number(id)) as DocRow | undefined;
      if (row) return documentToPaperShape(row);
    }
    if (hasTables(db, ["papers"])) {
      return (db.prepare(`SELECT * FROM papers WHERE id = ?`).get(id) as Paper) ?? null;
    }
    return null;
  } finally {
    db.close();
  }
}

export function getDocTypeCounts(siteIds?: string[]): DocTypeCount[] {
  if (getDbState(["documents"]).kind !== "ready") {
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
                 FROM documents
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
  if (getDbState(["documents"]).kind !== "ready") {
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
                `SELECT DISTINCT json_extract(metadata, '$.category') AS category
                 FROM documents
                 WHERE site_id IN (${resolvedSiteIds.map(() => "?").join(",")})
                   AND json_extract(metadata, '$.category') IS NOT NULL
                   AND json_extract(metadata, '$.category') != ''
                 ORDER BY category`
              )
              .all(...resolvedSiteIds) as { category: string }[])
          : [];
      return rows.map((r) => r.category).filter((c): c is string => Boolean(c));
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
  if (getDbState(["documents"]).kind !== "ready") {
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
      const total = (db.prepare(`SELECT COUNT(*) as c FROM documents`).get() as {
        c: number;
      }).c;
      const customPapers = (db
        .prepare(
          `SELECT COUNT(*) as c
           FROM documents
           WHERE site_id NOT IN ('nts-taxlaw-pd', 'nts-taxlaw-qt')`
        )
        .get() as { c: number }).c;
      const totalSites = (db
        .prepare(`SELECT COUNT(DISTINCT site_id) as c FROM documents`)
        .get() as { c: number }).c;
      const registeredSites = hasTables(db, ["sites"])
        ? (db.prepare(`SELECT COUNT(*) as c FROM sites`).get() as {
            c: number;
          }).c
        : 0;
      const last = db.prepare(`SELECT MAX(crawled_at) as last FROM documents`).get() as {
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
  if (getDbState(["documents"]).kind !== "ready") {
    return [];
  }

  return cached("sitesSummary", TTL, () => {
    const db = getDb();
    try {
      const rows = db
        .prepare(
          `SELECT d.site_id as site_id,
                  COALESCE(s.name, d.site_id) as site_name,
                  COUNT(d.id) as count,
                  MAX(d.crawled_at) as last_crawled
           FROM documents d
           LEFT JOIN sites s ON d.site_id = s.id
           GROUP BY d.site_id, COALESCE(s.name, d.site_id)
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
               ORDER BY crawled_at DESC, published_date DESC
               LIMIT ?`
            )
            .all(siteId, safeLimit) as DocRow[])
        : (db
            .prepare(
              `SELECT ${DOC_COLS_FULL}
               FROM documents
               ORDER BY crawled_at DESC, published_date DESC
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

// ====================================================================
// livertree: documents 테이블 (글로벌 INTEGER PK + 12자리 파일 매핑)
// docs/livertree.md 와 crawler/db.py 의 documents 스키마와 1:1 대응.
// ====================================================================

export interface LivertreeDocument {
  id: number;
  crawled_at: string;
  site_id: string;
  external_id: string | null;
  meta_url: string | null;
  title: string | null;
  published_date: string | null;
  posted_date: string | null;
  authors: string | null;       // "; " separated
  publisher: string | null;     // "; " separated
  journal: string | null;
  pdf_url: string | null;
  keywords: string | null;      // ", " separated
  abstract: string | null;
  original_filename: string | null;
  pdf_path: string | null;      // data/AAAA/BBBB/N.pdf
  txt_path: string | null;
  download_status: string | null;
  summary: string | null;
}

const DOCUMENT_COLUMNS = `
  id, crawled_at, site_id, external_id, meta_url, title,
  published_date, posted_date, authors, publisher, journal,
  pdf_url, keywords, abstract, original_filename,
  pdf_path, txt_path, download_status, summary
`;

export function getRecentDocuments(limit = 50): LivertreeDocument[] {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return [];
    return db
      .prepare(
        `SELECT ${DOCUMENT_COLUMNS} FROM documents
         ORDER BY id DESC
         LIMIT ?`
      )
      .all(limit) as LivertreeDocument[];
  } finally {
    db.close();
  }
}

export function getDocumentById(id: number): LivertreeDocument | null {
  const db = getDb();
  try {
    if (!hasTables(db, ["documents"])) return null;
    const row = db
      .prepare(`SELECT ${DOCUMENT_COLUMNS} FROM documents WHERE id = ?`)
      .get(id) as LivertreeDocument | undefined;
    return row ?? null;
  } finally {
    db.close();
  }
}

export interface SearchDocumentsParams {
  query?: string;        // matched against title/abstract/authors (LIKE)
  siteId?: string;
  limit?: number;
  offset?: number;
}

export function searchDocuments({
  query,
  siteId,
  limit = 50,
  offset = 0,
}: SearchDocumentsParams = {}): LivertreeDocument[] {
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
    return db
      .prepare(
        `SELECT ${DOCUMENT_COLUMNS} FROM documents
         ${where}
         ORDER BY id DESC
         LIMIT ? OFFSET ?`
      )
      .all(...params) as LivertreeDocument[];
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

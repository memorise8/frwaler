import fs from "fs";
import path from "path";
import Database from "better-sqlite3";

// Paths are env-driven; defaults point to the actual data locations.
// Keep separate from db.ts — this DB is large and read-only; never share state.
const PAPERS_DB_PATH =
  process.env.SOURCES_PAPERS_DB ??
  "/data_raid/ruci_workspace/crawler-poc/data/papers.db";

export const MD_ROOT =
  process.env.SOURCES_MD_ROOT ??
  "/data_raid/ruci_workspace/data/세법MD_20260417";

// Tables exposed via API — rejects anything not in this set.
export const ALLOWED_TABLES = new Set([
  "papers",
  "sites",
  "products",
  "doc_index",
]);

// FTS shadow tables and internal tables to exclude from overview.
const EXCLUDED_TABLE_PREFIXES = ["papers_fts", "sqlite_stat"];

export function isExcludedTable(name: string): boolean {
  return EXCLUDED_TABLE_PREFIXES.some((prefix) => name.startsWith(prefix));
}

// Open a fresh readonly connection each call. papers.db is 20GB — keep one
// connection open per request rather than a global singleton, to avoid issues
// with Next.js hot-reload and worker process lifecycle. Each call site is
// responsible for closing via finally.
export function getPapersDb(): InstanceType<typeof Database> {
  return new Database(PAPERS_DB_PATH, { readonly: true, fileMustExist: true });
}

export function papersDbExists(): boolean {
  return fs.existsSync(PAPERS_DB_PATH);
}

export function papersDbPath(): string {
  return PAPERS_DB_PATH;
}

/**
 * Sanitize a user query for FTS5 MATCH.
 * Each whitespace-separated token is wrapped in double-quotes (internal `"`
 * escaped as `""`). Returns null if there are no usable tokens.
 */
export function sanitizeFtsQuery(q: string): string | null {
  const tokens = q
    .trim()
    .split(/\s+/)
    .filter((t) => t.length > 0)
    .map((t) => `"${t.replace(/"/g, '""')}"`);
  return tokens.length > 0 ? tokens.join(" ") : null;
}

/**
 * Resolve a relative path from MD_ROOT, verifying it stays within MD_ROOT.
 * Returns the absolute path or null if it escapes the root (path traversal protection).
 */
export function resolveMdPath(relative: string): string | null {
  const normalized = relative.replace(/\\/g, "/").replace(/^\/+/, "");
  const absolute = path.resolve(MD_ROOT, normalized);
  if (!absolute.startsWith(path.resolve(MD_ROOT))) {
    return null;
  }
  return absolute;
}

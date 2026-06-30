import { getPapersDb, papersDbExists, papersDbPath, ALLOWED_TABLES, sanitizeFtsQuery } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;

export async function GET(request: Request) {
  const url = new URL(request.url);
  const table = url.searchParams.get("table") ?? "papers";
  const siteId = url.searchParams.get("siteId")?.trim() || undefined;
  const q = url.searchParams.get("q")?.trim() || undefined;
  const pageRaw = parseInt(url.searchParams.get("page") ?? "1", 10);
  const pageSizeRaw = parseInt(url.searchParams.get("pageSize") ?? String(DEFAULT_PAGE_SIZE), 10);

  if (!ALLOWED_TABLES.has(table)) {
    return Response.json({ error: "Invalid table name" }, { status: 400 });
  }

  if (!papersDbExists()) {
    return Response.json({ error: "papers.db not found", path: papersDbPath() }, { status: 404 });
  }

  const page = Math.max(1, Number.isFinite(pageRaw) ? pageRaw : 1);
  const pageSize = Math.min(MAX_PAGE_SIZE, Math.max(1, Number.isFinite(pageSizeRaw) ? pageSizeRaw : DEFAULT_PAGE_SIZE));
  const offset = (page - 1) * pageSize;

  const db = getPapersDb();
  try {
    // For non-papers tables, simple paginated select
    if (table !== "papers") {
      const total = (db.prepare(`SELECT COUNT(*) as c FROM "${table}"`).get() as { c: number }).c;
      const rows = db.prepare(`SELECT * FROM "${table}" LIMIT ? OFFSET ?`).all(pageSize, offset);
      return Response.json({ rows, total, page, pageSize });
    }

    // papers table — support FTS and siteId filter
    const ftsQuery = q ? sanitizeFtsQuery(q) : null;
    const hasFts = db.prepare(
      `SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'`
    ).get() != null;

    const where: string[] = [];
    const params: unknown[] = [];

    if (ftsQuery && hasFts) {
      // FTS5 path
      if (siteId) {
        where.push(`p.site_id = ?`);
        params.push(siteId);
      }
      const whereSQL = where.length ? `AND ${where.join(" AND ")}` : "";

      const ftsParams: unknown[] = [ftsQuery, ...params];
      const total = (
        db.prepare(
          `SELECT COUNT(*) as c FROM papers p
           JOIN papers_fts f ON p.rowid = f.rowid
           WHERE papers_fts MATCH ?
           ${whereSQL}`
        ).get(...ftsParams) as { c: number }
      ).c;

      const rows = db.prepare(
        `SELECT p.id, p.site_id, p.title, p.published_date, p.category,
                p.metadata, p.crawled_at, p.department, p.abstract
         FROM papers p
         JOIN papers_fts f ON p.rowid = f.rowid
         WHERE papers_fts MATCH ?
         ${whereSQL}
         ORDER BY p.published_date DESC, p.crawled_at DESC
         LIMIT ? OFFSET ?`
      ).all(...ftsParams, pageSize, offset);

      return Response.json({ rows, total, page, pageSize });
    }

    // Non-FTS path
    if (siteId) {
      where.push(`site_id = ?`);
      params.push(siteId);
    }
    if (q) {
      const pattern = `%${q}%`;
      where.push(`(COALESCE(title, '') LIKE ? OR COALESCE(abstract, '') LIKE ?)`);
      params.push(pattern, pattern);
    }

    const whereSQL = where.length ? `WHERE ${where.join(" AND ")}` : "";

    const total = (
      db.prepare(`SELECT COUNT(*) as c FROM papers ${whereSQL}`).get(...params) as { c: number }
    ).c;

    const rows = db.prepare(
      `SELECT id, site_id, title, published_date, category,
              metadata, crawled_at, department, abstract
       FROM papers ${whereSQL}
       ORDER BY published_date DESC, crawled_at DESC
       LIMIT ? OFFSET ?`
    ).all(...params, pageSize, offset);

    return Response.json({ rows, total, page, pageSize });
  } finally {
    db.close();
  }
}

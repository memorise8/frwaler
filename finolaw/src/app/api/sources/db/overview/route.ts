import { getPapersDb, papersDbExists, papersDbPath, isExcludedTable } from "@/lib/sources";
import fs from "fs";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  if (!papersDbExists()) {
    return Response.json({ error: "papers.db not found", path: papersDbPath() }, { status: 404 });
  }

  const db = getPapersDb();
  try {
    const tableRows = db
      .prepare(
        `SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`
      )
      .all() as { name: string }[];

    const tables = tableRows
      .filter((r) => !isExcludedTable(r.name))
      .map((r) => {
        const count = (
          db.prepare(`SELECT COUNT(*) as c FROM "${r.name}"`).get() as { c: number }
        ).c;
        return { name: r.name, rowCount: count };
      });

    const stat = fs.statSync(papersDbPath());
    const dbSize = stat.size;

    const lastCrawled = (() => {
      try {
        const row = db
          .prepare(`SELECT MAX(crawled_at) as last FROM papers`)
          .get() as { last: string | null };
        return row.last;
      } catch {
        return null;
      }
    })();

    return Response.json({ tables, dbSize, lastCrawled, path: papersDbPath() });
  } finally {
    db.close();
  }
}

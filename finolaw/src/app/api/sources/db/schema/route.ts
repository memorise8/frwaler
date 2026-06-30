import { getPapersDb, papersDbExists, papersDbPath, ALLOWED_TABLES } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const table = url.searchParams.get("table") ?? "";

  if (!ALLOWED_TABLES.has(table)) {
    return Response.json({ error: "Invalid table name" }, { status: 400 });
  }

  if (!papersDbExists()) {
    return Response.json({ error: "papers.db not found", path: papersDbPath() }, { status: 404 });
  }

  const db = getPapersDb();
  try {
    const columns = db
      .prepare(`PRAGMA table_info("${table}")`)
      .all() as Array<{ cid: number; name: string; type: string; notnull: number; dflt_value: unknown; pk: number }>;

    return Response.json({ table, columns: columns.map((c) => ({
      name: c.name,
      type: c.type,
      notnull: c.notnull === 1,
      pk: c.pk > 0,
    })) });
  } finally {
    db.close();
  }
}

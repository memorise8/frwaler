import { getPapersDb, papersDbExists, papersDbPath, ALLOWED_TABLES } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const table = url.searchParams.get("table") ?? "papers";
  const id = url.searchParams.get("id") ?? "";

  if (!ALLOWED_TABLES.has(table)) {
    return Response.json({ error: "Invalid table name" }, { status: 400 });
  }
  if (!id) {
    return Response.json({ error: "id required" }, { status: 400 });
  }

  if (!papersDbExists()) {
    return Response.json({ error: "papers.db not found", path: papersDbPath() }, { status: 404 });
  }

  const db = getPapersDb();
  try {
    const row = db.prepare(`SELECT * FROM "${table}" WHERE id = ?`).get(id) as Record<string, unknown> | undefined;
    if (!row) {
      return Response.json({ error: "Row not found" }, { status: 404 });
    }

    // Parse metadata JSON if present
    if (typeof row.metadata === "string") {
      try {
        row.metadata = JSON.parse(row.metadata);
      } catch {
        // leave as string
      }
    }
    if (typeof row.authors === "string") {
      try {
        row.authors = JSON.parse(row.authors);
      } catch {
        // leave as string
      }
    }
    if (typeof row.keywords === "string") {
      try {
        row.keywords = JSON.parse(row.keywords);
      } catch {
        // leave as string
      }
    }

    return Response.json({ row });
  } finally {
    db.close();
  }
}

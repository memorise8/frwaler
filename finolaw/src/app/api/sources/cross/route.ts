import fs from "fs";
import path from "path";
import { getPapersDb, papersDbExists, MD_ROOT } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Recursively find .md files whose basename (without .md) matches docNumber.
// Stops after finding 5 matches to avoid scanning all 471k files.
function findMdFiles(dir: string, docNumber: string, results: string[], limit = 5): void {
  if (results.length >= limit) return;
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (results.length >= limit) break;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      findMdFiles(fullPath, docNumber, results, limit);
    } else if (entry.isFile() && entry.name === `${docNumber}.md`) {
      results.push(path.relative(MD_ROOT, fullPath));
    }
  }
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const docNumber = url.searchParams.get("docNumber")?.trim() ?? "";

  if (!docNumber) {
    return Response.json({ error: "docNumber required" }, { status: 400 });
  }

  // Search DB
  let dbRows: unknown[] = [];
  if (papersDbExists()) {
    const db = getPapersDb();
    try {
      dbRows = db.prepare(
        `SELECT id, site_id, title, published_date, metadata
         FROM papers
         WHERE json_extract(metadata, '$.documentNumber') = ?
         LIMIT 10`
      ).all(docNumber);
    } finally {
      db.close();
    }
  }

  // Search MD tree
  const mdPaths: string[] = [];
  findMdFiles(MD_ROOT, docNumber, mdPaths, 5);

  return Response.json({ docNumber, dbRows, mdPaths });
}

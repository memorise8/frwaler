import fs from "fs";
import path from "path";
import { resolveMdPath, MD_ROOT } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MANY_THRESHOLD = 500;

export async function GET(request: Request) {
  const url = new URL(request.url);
  const rel = url.searchParams.get("path") ?? "";

  const absolute = resolveMdPath(rel);
  if (!absolute) {
    return Response.json({ error: "Invalid path" }, { status: 400 });
  }

  if (!fs.existsSync(absolute)) {
    return Response.json({ error: "Path not found" }, { status: 404 });
  }

  const stat = fs.statSync(absolute);
  if (!stat.isDirectory()) {
    return Response.json({ error: "Not a directory" }, { status: 400 });
  }

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(absolute, { withFileTypes: true });
  } catch {
    return Response.json({ error: "Cannot read directory" }, { status: 500 });
  }

  const children = entries
    .sort((a, b) => a.name.localeCompare(b.name, "ko"))
    .map((entry) => {
      const childAbs = path.join(absolute, entry.name);
      const relPath = path.relative(MD_ROOT, childAbs);

      if (entry.isDirectory()) {
        // Count direct children cheaply; if very large just say "많음"
        let count: number | null = null;
        let countLabel: string | undefined;
        try {
          const sub = fs.readdirSync(childAbs);
          if (sub.length >= MANY_THRESHOLD) {
            countLabel = "다수";
          } else {
            count = sub.length;
          }
        } catch {
          // skip count
        }
        return { name: entry.name, kind: "dir" as const, relPath, count, countLabel };
      } else if (entry.isFile() && entry.name.endsWith(".md")) {
        return { name: entry.name, kind: "file" as const, relPath };
      }
      return null;
    })
    .filter((x): x is NonNullable<typeof x> => x !== null);

  return Response.json({ children, relPath: path.relative(MD_ROOT, absolute) });
}

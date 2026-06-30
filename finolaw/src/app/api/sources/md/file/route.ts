import fs from "fs";
import path from "path";
import { resolveMdPath } from "@/lib/sources";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MAX_BYTES = 1024 * 1024; // 1MB cap

export async function GET(request: Request) {
  const url = new URL(request.url);
  const rel = url.searchParams.get("path") ?? "";

  const absolute = resolveMdPath(rel);
  if (!absolute) {
    return Response.json({ error: "Invalid path" }, { status: 400 });
  }

  if (!fs.existsSync(absolute)) {
    return Response.json({ error: "File not found" }, { status: 404 });
  }

  const stat = fs.statSync(absolute);
  if (!stat.isFile()) {
    return Response.json({ error: "Not a file" }, { status: 400 });
  }

  const sizeBytes = stat.size;
  const basename = path.basename(absolute);
  const ext = path.extname(basename).toLowerCase();
  if (ext !== ".md") {
    return Response.json({ error: "Only .md files are served" }, { status: 400 });
  }

  let content: string;
  if (sizeBytes > MAX_BYTES) {
    const buf = Buffer.alloc(MAX_BYTES);
    const fd = fs.openSync(absolute, "r");
    try {
      fs.readSync(fd, buf, 0, MAX_BYTES, 0);
    } finally {
      fs.closeSync(fd);
    }
    content = buf.toString("utf8") + "\n\n…(1MB에서 잘림)";
  } else {
    content = fs.readFileSync(absolute, "utf8");
  }

  return Response.json({ content, sizeBytes, basename, absolutePath: absolute });
}

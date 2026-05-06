import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { listRunningJobs } from "@/lib/crawler-runner";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BUILT_IN = ["nts-taxlaw-pd", "nts-taxlaw-qt", "ntrs", "mohw", "fsc"];
const SITE_ID_RE = /^[a-z0-9][a-z0-9\-_]{1,63}$/;

type SiteKind = "built-in" | "custom" | "config";

interface SiteEntry {
  id: string;
  kind: SiteKind;
  deletable: boolean;
  fileName?: string;
}

function scanConfigDir(dir: string): SiteEntry[] {
  try {
    return fs
      .readdirSync(dir)
      .filter((f) => f.endsWith(".json") && !f.startsWith("__"))
      .map((fileName) => {
        const filePath = path.join(dir, fileName);
        try {
          const raw = fs.readFileSync(filePath, "utf8");
          const parsed = JSON.parse(raw) as { site_id?: unknown };
          const id = typeof parsed.site_id === "string"
            ? parsed.site_id
            : fileName.slice(0, -".json".length);
          return { id, kind: "config" as const, deletable: true, fileName };
        } catch {
          return {
            id: fileName.slice(0, -".json".length),
            kind: "config" as const,
            deletable: true,
            fileName,
          };
        }
      });
  } catch {
    return [];
  }
}

function scanCustomDir(dir: string): SiteEntry[] {
  try {
    return fs
      .readdirSync(dir)
      .filter((f) => f.endsWith(".py") && f !== "__init__.py")
      .map((fileName) => {
        const filePath = path.join(dir, fileName);
        let id = fileName.slice(0, -".py".length);
        try {
          const raw = fs.readFileSync(filePath, "utf8");
          const match = raw.match(/\bsite_id\s*=\s*["']([^"']+)["']/);
          if (match?.[1]) id = match[1];
        } catch {
          // fall back to file name
        }
        return { id, kind: "custom" as const, deletable: true, fileName };
      });
  } catch {
    return [];
  }
}

function getSites() {
  const base = path.resolve(process.cwd(), "..", "crawler", "sites");
  const entries: SiteEntry[] = [
    ...BUILT_IN.map((id) => ({ id, kind: "built-in" as const, deletable: false })),
    ...scanConfigDir(path.join(base, "configs")),
    ...scanCustomDir(path.join(base, "custom")),
  ];

  const byId = new Map<string, SiteEntry>();
  for (const entry of entries) {
    if (!byId.has(entry.id)) {
      byId.set(entry.id, entry);
    }
  }

  const sites = [...byId.values()].sort((a, b) => a.id.localeCompare(b.id));
  return { base, sites };
}

export async function GET() {
  const { sites } = getSites();
  return NextResponse.json({
    sites: sites.map((site) => site.id),
    siteDetails: sites,
  });
}

export async function DELETE(req: Request) {
  let body: { siteId?: unknown };
  try {
    body = (await req.json()) as { siteId?: unknown };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }

  const siteId = typeof body.siteId === "string" ? body.siteId : "";
  if (!SITE_ID_RE.test(siteId)) {
    return NextResponse.json({ error: "invalid siteId" }, { status: 400 });
  }

  const { base, sites } = getSites();
  const site = sites.find((item) => item.id === siteId);
  if (!site) {
    return NextResponse.json({ error: "site not found" }, { status: 404 });
  }
  if (!site.deletable || site.kind === "built-in") {
    return NextResponse.json(
      { error: "built-in crawler cannot be deleted" },
      { status: 403 }
    );
  }
  if (listRunningJobs().some((job) => job.siteId === siteId)) {
    return NextResponse.json(
      { error: "running crawler cannot be deleted" },
      { status: 409 }
    );
  }
  if (!site.fileName) {
    return NextResponse.json({ error: "site file not found" }, { status: 404 });
  }

  const dirName = site.kind === "custom" ? "custom" : "configs";
  const filePath = path.resolve(base, dirName, site.fileName);
  const expectedDir = path.resolve(base, dirName);
  if (!filePath.startsWith(`${expectedDir}${path.sep}`)) {
    return NextResponse.json({ error: "invalid site path" }, { status: 400 });
  }

  try {
    fs.unlinkSync(filePath);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "delete failed" },
      { status: 500 }
    );
  }

  return NextResponse.json({ deleted: true, siteId, kind: site.kind });
}

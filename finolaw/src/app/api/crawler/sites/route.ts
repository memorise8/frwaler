import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { getSiteOptionsRich } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function scanDir(dir: string, suffix: string): string[] {
  try {
    return fs
      .readdirSync(dir)
      .filter((f) => f.endsWith(suffix) && !f.startsWith("__"))
      .map((f) => f.slice(0, -suffix.length));
  } catch {
    return [];
  }
}

/**
 * Sites available for crawler runs. Sources, in priority order:
 *   1. libertree.db — sites already collected (authoritative for crawler ops)
 *   2. crawler/sites/configs/*.json — declarative configs on disk
 *   3. crawler/sites/custom/*.py    — hand-written custom crawlers
 *
 * Legacy hard-coded built-ins are no longer injected; sites only show up
 * if they're actually present on disk or in the DB.
 */
export async function GET() {
  const base = path.resolve(process.cwd(), "..", "crawler", "sites");
  const configs = scanDir(path.join(base, "configs"), ".json");
  const custom = scanDir(path.join(base, "custom"), ".py");

  let dbSites: string[] = [];
  try {
    dbSites = getSiteOptionsRich().map((s) => s.site_id);
  } catch {
    // DB unavailable — fall through to disk-only listing.
  }

  const all = Array.from(new Set([...dbSites, ...configs, ...custom])).sort();
  return NextResponse.json({ sites: all });
}

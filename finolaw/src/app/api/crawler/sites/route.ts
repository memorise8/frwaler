import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BUILT_IN = ["nts-taxlaw-pd", "nts-taxlaw-qt", "ntrs", "mohw", "fsc"];

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

export async function GET() {
  const base = path.resolve(process.cwd(), "..", "crawler", "sites");
  const configs = scanDir(path.join(base, "configs"), ".json");
  const custom = scanDir(path.join(base, "custom"), ".py");
  const all = Array.from(new Set([...BUILT_IN, ...configs, ...custom])).sort();
  return NextResponse.json({ sites: all });
}

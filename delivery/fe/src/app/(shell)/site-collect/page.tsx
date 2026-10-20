import { operatorHeaders } from "@/lib/backend-auth";
import { normalizeRunnerHealth, normalizeSitesReport, normalizeUncollected } from "@/lib/site-collect";
import SiteCollectConsole from "./site-collect-console";

export const dynamic = "force-dynamic";

async function readCollect(path: string): Promise<unknown> {
  try {
    const base = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${base}/collect/${path}`, {
      headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(15_000),
    });
    return response.ok ? await response.json() as unknown : null;
  } catch { return null; }
}

export default async function SiteCollectPage() {
  const [sites, uncollected, runner] = await Promise.all([
    readCollect("sites"), readCollect("uncollected"), readCollect("runner/health"),
  ]);
  return <SiteCollectConsole
    report={sites === null ? null : normalizeSitesReport(sites)}
    uncollected={normalizeUncollected(uncollected)}
    runner={normalizeRunnerHealth(runner)}
  />;
}

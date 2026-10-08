import { operatorHeaders } from "@/lib/backend-auth";
import type { CrawlerHealthReport, SystemHealthReport } from "@/lib/health-status";
import HealthDashboard from "./health-dashboard";

export const dynamic = "force-dynamic";

async function readHealth<T>(path: string): Promise<T | null> {
  try {
    const base = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${base}/health/${path}`, {
      headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(15_000),
    });
    return response.ok ? await response.json() as T : null;
  } catch { return null; }
}

export default async function HealthPage() {
  const [crawlers, system] = await Promise.all([
    readHealth<CrawlerHealthReport>("crawlers"), readHealth<SystemHealthReport>("system"),
  ]);
  return <HealthDashboard crawlers={crawlers} system={system} />;
}

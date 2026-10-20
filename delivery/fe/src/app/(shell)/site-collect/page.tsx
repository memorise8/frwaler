import { operatorHeaders } from "@/lib/backend-auth";
import { buildConsole, type Taxonomy } from "@/lib/collect-console";
import { getCrawlerHealth } from "@/lib/crawler-health";
import taxonomy from "@/vendor/site-taxonomy.json";
import SiteCollectConsole from "./site-collect-console";

export const dynamic = "force-dynamic";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// 실패(연결 안 됨·인증 실패·오류 응답)는 null 로 둔다. 화면은 읽은 것만 보여 주고
// 못 읽은 원천을 위쪽 오류 줄에 적는다.
async function read(path: string): Promise<unknown> {
  try {
    const response = await fetch(`${backendUrl()}${path}`, {
      headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(15_000),
    });
    return response.ok ? await response.json() as unknown : null;
  } catch { return null; }
}

const crawlerNames = (): ReadonlyMap<string, string> => {
  try {
    return new Map(getCrawlerHealth().filter((row) => row.siteName).map((row) => [row.siteId, row.siteName]));
  } catch { return new Map(); }
};

// 조회 시각은 렌더 밖에서 정한다(렌더는 순수해야 한다).
async function loadConsole() {
  const [sites, uncollected, runs, health, stats, progress] = await Promise.all([
    read("/collect/sites"), read("/collect/uncollected"), read("/collect/runs"), read("/collect/runner/health"),
    read("/stats"), read("/progress"),
  ]);
  const now = Date.now() / 1000;
  return { now, data: buildConsole({ sites, uncollected, runs, health, stats, progress }, taxonomy as Taxonomy, crawlerNames(), now) };
}

export default async function SiteCollectPage() {
  const { data, now } = await loadConsole();
  return <SiteCollectConsole data={data} fetchedAt={now} />;
}

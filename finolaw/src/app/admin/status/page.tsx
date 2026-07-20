import Link from "next/link";
import {
  getCollectionProgress,
  getCollectionTotals,
  getSiteOptionsRich,
  type SiteProgress,
} from "@/lib/db";
import { getCategoryForSheet } from "@/lib/categories";

export const dynamic = "force-dynamic";

type GroupMode = "site" | "continent" | "country" | "category";

function parseGroupBy(s: string | undefined): GroupMode {
  if (s === "continent" || s === "country" || s === "category") return s;
  return "site";
}

interface Props {
  searchParams: Promise<{ groupBy?: string }>;
}

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function pct(part: number, whole: number): number {
  if (!whole) return 0;
  return Math.min(100, Math.round((part / whole) * 100));
}

interface GroupedRow {
  key: string;
  label: string;
  total: number;
  downloaded: number;
  converted: number;
  summarized: number;
  last_crawled: string | null;
  sites: SiteProgress[];
}

function groupRows(
  rows: SiteProgress[],
  mode: GroupMode,
  enrichment: Map<string, { sheet: string | null; country: string; continent: string; category: string }>,
): GroupedRow[] {
  if (mode === "site") {
    return rows.map((r) => ({
      key: r.site_id,
      label: r.site_name,
      total: r.total,
      downloaded: r.downloaded,
      converted: r.converted,
      summarized: r.summarized,
      last_crawled: r.last_crawled,
      sites: [r],
    }));
  }

  const buckets = new Map<string, GroupedRow>();
  for (const r of rows) {
    const meta = enrichment.get(r.site_id);
    const key =
      mode === "continent"
        ? meta?.continent ?? "Other"
        : mode === "country"
          ? meta?.country ?? "기타"
          : meta?.category ?? "기타";
    const existing = buckets.get(key);
    if (existing) {
      existing.total += r.total;
      existing.downloaded += r.downloaded;
      existing.converted += r.converted;
      existing.summarized += r.summarized;
      existing.sites.push(r);
      if (
        r.last_crawled &&
        (!existing.last_crawled || r.last_crawled > existing.last_crawled)
      ) {
        existing.last_crawled = r.last_crawled;
      }
    } else {
      buckets.set(key, {
        key,
        label: key,
        total: r.total,
        downloaded: r.downloaded,
        converted: r.converted,
        summarized: r.summarized,
        last_crawled: r.last_crawled,
        sites: [r],
      });
    }
  }
  return Array.from(buckets.values()).sort((a, b) => b.total - a.total);
}

export default async function AdminStatusPage({ searchParams }: Props) {
  const sp = await searchParams;
  const groupBy = parseGroupBy(sp.groupBy);

  let rows: SiteProgress[] = [];
  let totals: ReturnType<typeof getCollectionTotals> = {
    total: 0,
    downloaded: 0,
    converted: 0,
    summarized: 0,
    sites: 0,
  };
  let dbError: string | null = null;
  const enrichment = new Map<string, { sheet: string | null; country: string; continent: string; category: string }>();
  try {
    rows = getCollectionProgress();
    totals = getCollectionTotals();
    for (const s of getSiteOptionsRich()) {
      enrichment.set(s.site_id, {
        sheet: s.sheet,
        country: s.country,
        continent: s.continent,
        category: s.category,
      });
    }
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  if (dbError) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">수집 현황</h1>
        <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-6 text-red-700 dark:text-red-300">
          <p className="font-semibold">데이터베이스 연결 실패</p>
          <p className="text-sm mt-2">{dbError}</p>
          <p className="text-xs mt-3 font-mono opacity-75">
            예상 경로: ../data/libertree.db
          </p>
        </div>
      </div>
    );
  }

  const grouped = groupRows(rows, groupBy, enrichment);

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold">수집 현황</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            사이트별 수집 → 다운로드 → 변환 → 요약 진행률을 한눈에 확인합니다.
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {(["site", "continent", "country", "category"] as GroupMode[]).map((mode) => (
            <Link
              key={mode}
              href={mode === "site" ? "/admin/status" : `/admin/status?groupBy=${mode}`}
              className={`px-3 py-1.5 rounded-lg border ${
                groupBy === mode
                  ? "bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 border-slate-900 dark:border-slate-100"
                  : "border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800"
              }`}
            >
              {mode === "site"
                ? "사이트"
                : mode === "continent"
                  ? "대륙"
                  : mode === "country"
                    ? "국가"
                    : "범주"}
            </Link>
          ))}
        </div>
      </div>

      {totals.total === 0 && (
        <div className="rounded-xl border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 p-5 text-amber-900 dark:text-amber-200">
          <p className="font-semibold">아직 수집된 문서가 없습니다.</p>
          <p className="text-sm mt-2">
            <code className="font-mono">python -m crawler.main crawl &lt;site&gt;</code> 로 첫 수집을 시작해 주세요.
          </p>
        </div>
      )}

      <section className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Stat label="활성 사이트" value={formatNum(totals.sites)} tone="indigo" />
        <Stat label="수집 문서" value={formatNum(totals.total)} tone="indigo" />
        <Stat
          label="다운로드 완료"
          value={`${formatNum(totals.downloaded)} (${pct(totals.downloaded, totals.total)}%)`}
          tone="blue"
        />
        <Stat
          label="텍스트 변환"
          value={`${formatNum(totals.converted)} (${pct(totals.converted, totals.total)}%)`}
          tone="emerald"
        />
        <Stat
          label="AI 요약"
          value={`${formatNum(totals.summarized)} (${pct(totals.summarized, totals.total)}%)`}
          tone="amber"
        />
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">
          {groupBy === "site"
            ? "사이트별 진행률"
            : groupBy === "continent"
              ? "대륙별 진행률"
              : groupBy === "country"
                ? "국가별 진행률"
                : "범주별 진행률"}
        </h2>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
              <tr>
                <th className="text-left px-4 py-3 font-medium">
                  {groupBy === "site" ? "사이트" : "그룹"}
                </th>
                {groupBy !== "site" && (
                  <th className="text-right px-4 py-3 font-medium">사이트 수</th>
                )}
                <th className="text-right px-4 py-3 font-medium">수집</th>
                <th className="text-left px-4 py-3 font-medium">다운로드</th>
                <th className="text-left px-4 py-3 font-medium">변환</th>
                <th className="text-left px-4 py-3 font-medium">요약</th>
                <th className="text-right px-4 py-3 font-medium">최근</th>
                <th className="text-right px-4 py-3 font-medium">작업</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {grouped.length === 0 ? (
                <tr>
                  <td
                    colSpan={groupBy === "site" ? 7 : 8}
                    className="px-4 py-10 text-center text-slate-400 dark:text-slate-500"
                  >
                    표시할 사이트가 없습니다.
                  </td>
                </tr>
              ) : (
                grouped.map((row) => {
                  const meta = groupBy === "site" ? enrichment.get(row.sites[0].site_id) : null;
                  const cat = meta ? getCategoryForSheet(meta.sheet) : null;
                  return (
                    <tr key={row.key} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                      <td className="px-4 py-2.5">
                        <div className="font-medium">{row.label}</div>
                        {groupBy === "site" && (
                          <div className="font-mono text-xs text-slate-500 dark:text-slate-400">
                            {row.sites[0].site_id}
                            {cat && (
                              <>
                                {" "}· <span>{cat.country}</span>{" / "}
                                <span>{cat.category}</span>
                              </>
                            )}
                          </div>
                        )}
                      </td>
                      {groupBy !== "site" && (
                        <td className="px-4 py-2.5 text-right font-mono">
                          {formatNum(row.sites.length)}
                        </td>
                      )}
                      <td className="px-4 py-2.5 text-right font-mono">{formatNum(row.total)}</td>
                      <td className="px-4 py-2.5">
                        <ProgressBar part={row.downloaded} whole={row.total} tone="blue" />
                      </td>
                      <td className="px-4 py-2.5">
                        <ProgressBar part={row.converted} whole={row.total} tone="emerald" />
                      </td>
                      <td className="px-4 py-2.5">
                        <ProgressBar part={row.summarized} whole={row.total} tone="amber" />
                      </td>
                      <td className="px-4 py-2.5 text-right text-xs text-slate-500 dark:text-slate-400">
                        {formatDate(row.last_crawled)}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        {groupBy === "site" ? (
                          <Link
                            href={`/search?site=${encodeURIComponent(row.sites[0].site_id)}`}
                            className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                          >
                            보기
                          </Link>
                        ) : (
                          <Link
                            href={
                              groupBy === "continent"
                                ? `/search?continent=${encodeURIComponent(row.label)}`
                                : groupBy === "country"
                                  ? `/search?country=${encodeURIComponent(row.label)}`
                                  : `/search?category=${encodeURIComponent(row.label)}`
                            }
                            className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                          >
                            검색
                          </Link>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "indigo" | "blue" | "emerald" | "amber";
}) {
  const toneClass = {
    indigo:
      "bg-indigo-50 dark:bg-indigo-950/40 border-indigo-200 dark:border-indigo-900 text-indigo-900 dark:text-indigo-200",
    blue: "bg-blue-50 dark:bg-blue-950/40 border-blue-200 dark:border-blue-900 text-blue-900 dark:text-blue-200",
    emerald:
      "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900 text-emerald-900 dark:text-emerald-200",
    amber:
      "bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-900 text-amber-900 dark:text-amber-200",
  }[tone];
  return (
    <div className={`rounded-xl border p-5 ${toneClass}`}>
      <div className="text-xs font-medium opacity-75">{label}</div>
      <div className="text-2xl font-bold tabular-nums mt-1">{value}</div>
    </div>
  );
}

function ProgressBar({
  part,
  whole,
  tone,
}: {
  part: number;
  whole: number;
  tone: "blue" | "emerald" | "amber";
}) {
  const p = pct(part, whole);
  const fill = {
    blue: "bg-blue-500",
    emerald: "bg-emerald-500",
    amber: "bg-amber-500",
  }[tone];
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
        <div className={`h-full ${fill}`} style={{ width: `${p}%` }} />
      </div>
      <span className="text-xs text-slate-500 dark:text-slate-400 w-20 text-right tabular-nums font-mono">
        {formatNum(part)} ({p}%)
      </span>
    </div>
  );
}

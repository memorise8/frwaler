import Link from "next/link";
import {
  getCollectionProgress,
  getCollectionTotals,
  type SiteProgress,
} from "@/lib/db";

export const dynamic = "force-dynamic";

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

export default async function AdminStatusPage() {
  let rows: SiteProgress[] = [];
  let totals: ReturnType<typeof getCollectionTotals> = {
    total: 0,
    downloaded: 0,
    converted: 0,
    summarized: 0,
    sites: 0,
  };
  let dbError: string | null = null;
  try {
    rows = getCollectionProgress();
    totals = getCollectionTotals();
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
            예상 경로: ../data/papers.db
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">수집 현황</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          사이트별 수집 → 다운로드 → 변환 → 요약 진행률을 한눈에 확인합니다.
        </p>
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
        <Stat
          label="활성 사이트"
          value={formatNum(totals.sites)}
          tone="indigo"
        />
        <Stat
          label="수집 문서"
          value={formatNum(totals.total)}
          tone="indigo"
        />
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
        <h2 className="text-lg font-semibold mb-3">사이트별 진행률</h2>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
              <tr>
                <th className="text-left px-4 py-3 font-medium">사이트</th>
                <th className="text-right px-4 py-3 font-medium">수집</th>
                <th className="text-left px-4 py-3 font-medium">다운로드</th>
                <th className="text-left px-4 py-3 font-medium">변환</th>
                <th className="text-left px-4 py-3 font-medium">요약</th>
                <th className="text-right px-4 py-3 font-medium">최근</th>
                <th className="text-right px-4 py-3 font-medium">작업</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {rows.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-10 text-center text-slate-400 dark:text-slate-500"
                  >
                    표시할 사이트가 없습니다.
                  </td>
                </tr>
              ) : (
                rows.map((r) => (
                  <tr
                    key={r.site_id}
                    className="hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  >
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{r.site_name}</div>
                      <div className="font-mono text-xs text-slate-500 dark:text-slate-400">
                        {r.site_id}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono">
                      {formatNum(r.total)}
                    </td>
                    <td className="px-4 py-2.5">
                      <ProgressBar part={r.downloaded} whole={r.total} tone="blue" />
                    </td>
                    <td className="px-4 py-2.5">
                      <ProgressBar part={r.converted} whole={r.total} tone="emerald" />
                    </td>
                    <td className="px-4 py-2.5">
                      <ProgressBar part={r.summarized} whole={r.total} tone="amber" />
                    </td>
                    <td className="px-4 py-2.5 text-right text-xs text-slate-500 dark:text-slate-400">
                      {formatDate(r.last_crawled)}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Link
                        href={`/search?site=${encodeURIComponent(r.site_id)}`}
                        className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                      >
                        보기
                      </Link>
                    </td>
                  </tr>
                ))
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

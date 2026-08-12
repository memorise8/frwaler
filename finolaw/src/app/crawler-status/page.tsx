import type { Metadata } from "next";
import Link from "next/link";
import {
  CRAWLER_AUDIT_DATE,
  getCrawlerHealthRows,
  type CrawlerHealthRow,
  type CrawlerHealthStatus,
} from "@/lib/crawler-health";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "크롤러 상태 | Libertree" };

const PAGE_SIZE = 50;

type SearchParams = Promise<{
  readonly q?: string | string[];
  readonly status?: string | string[];
  readonly category?: string | string[];
  readonly page?: string | string[];
}>;

const scalar = (value: string | string[] | undefined): string =>
  Array.isArray(value) ? value[0] ?? "" : value ?? "";

const statusLabel: Record<CrawlerHealthStatus, string> = {
  healthy: "정상",
  unhealthy: "실패",
};

const hrefFor = (
  current: Readonly<{ q: string; status: string; category: string }>,
  page: number,
): string => {
  const params = new URLSearchParams();
  if (current.q) params.set("q", current.q);
  if (current.status) params.set("status", current.status);
  if (current.category) params.set("category", current.category);
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `/crawler-status?${query}` : "/crawler-status";
};

const StatCard = ({
  caption,
  count,
  label,
  tone,
}: Readonly<{ caption: string; count: number; label: string; tone: "green" | "red" | "slate" }>) => {
  const tones = {
    green: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300",
    red: "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300",
    slate: "border-slate-200 bg-white text-slate-900 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100",
  } as const;
  return (
    <div className={`rounded-xl border p-5 ${tones[tone]}`}>
      <p className="text-xs font-semibold uppercase tracking-wider opacity-70">{label}</p>
      <p className="mt-2 text-3xl font-bold tabular-nums">{count.toLocaleString("ko-KR")}</p>
      <p className="mt-1 text-xs opacity-70">{caption}</p>
    </div>
  );
};

const StatusBadge = ({ row }: Readonly<{ row: CrawlerHealthRow }>) => (
  <span
    className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${
      row.status === "healthy"
        ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
        : "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
    }`}
  >
    <span className={`h-2 w-2 rounded-full ${row.status === "healthy" ? "bg-emerald-500" : "bg-red-500"}`} />
    {statusLabel[row.status]}
  </span>
);

export default async function CrawlerStatusPage({ searchParams }: Readonly<{ searchParams: SearchParams }>) {
  const params = await searchParams;
  const q = scalar(params.q).trim().toLocaleLowerCase("ko-KR");
  const status = scalar(params.status);
  const category = scalar(params.category);
  const requestedPage = Number.parseInt(scalar(params.page), 10) || 1;

  const rows = getCrawlerHealthRows();
  const healthy = rows.filter((row) => row.status === "healthy").length;
  const unhealthy = rows.length - healthy;
  const categories = [...new Set(rows.map((row) => row.category).filter(Boolean))].sort();
  const filtered = rows.filter((row) => {
    const matchesQuery = !q || `${row.siteId} ${row.siteName} ${row.reason}`.toLocaleLowerCase("ko-KR").includes(q);
    return matchesQuery && (!status || row.status === status) && (!category || row.category === category);
  });
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const page = Math.min(Math.max(1, requestedPage), pageCount);
  const visible = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const current = { q: scalar(params.q).trim(), status, category };

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs font-semibold tracking-[0.16em] text-emerald-700 dark:text-emerald-400">CRAWLER HEALTH</p>
          <h1 className="mt-1 text-2xl font-bold sm:text-3xl">크롤러 상태</h1>
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            실제 미니 크롤로 문서 저장 여부를 확인한 마지막 검증 결과입니다.
          </p>
        </div>
        <div className="text-right text-xs text-slate-500 dark:text-slate-400">
          <p>마지막 검증</p>
          <p className="mt-1 font-mono text-slate-700 dark:text-slate-200">{CRAWLER_AUDIT_DATE}</p>
        </div>
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label="전체 크롤러" count={rows.length} caption="상태 카탈로그 등록" tone="slate" />
        <StatCard label="정상" count={healthy} caption={`${rows.length ? ((healthy / rows.length) * 100).toFixed(1) : "0.0"}% 실제 저장 성공`} tone="green" />
        <StatCard label="실패·확인 필요" count={unhealthy} caption="코드·차단·네트워크 포함" tone="red" />
      </div>

      <aside className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
        이 화면은 실시간 상태가 아닌 <strong>{CRAWLER_AUDIT_DATE}</strong> 기준 스냅샷입니다. IP 차단은 고객사 네트워크에서 결과가 달라질 수 있습니다.
      </aside>

      <form className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900" action="/crawler-status">
        <div className="grid gap-3 md:grid-cols-[minmax(16rem,1fr)_12rem_12rem_auto]">
          <label className="space-y-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
            검색
            <input
              className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-normal text-slate-900 outline-none focus:border-emerald-600 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
              defaultValue={scalar(params.q)}
              name="q"
              placeholder="사이트 이름 또는 site_id"
            />
          </label>
          <label className="space-y-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
            상태
            <select className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-normal dark:border-slate-700 dark:bg-slate-950" defaultValue={status} name="status">
              <option value="">전체</option>
              <option value="healthy">정상</option>
              <option value="unhealthy">실패</option>
            </select>
          </label>
          <label className="space-y-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
            실패 유형
            <select className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-normal dark:border-slate-700 dark:bg-slate-950" defaultValue={category} name="category">
              <option value="">전체</option>
              {categories.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <div className="flex items-end gap-2">
            <button className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-700 dark:bg-slate-700 dark:hover:bg-slate-600" type="submit">조회</button>
            <Link className="px-2 py-2.5 text-sm text-slate-500 hover:text-slate-900 dark:hover:text-white" href="/crawler-status">초기화</Link>
          </div>
        </div>
      </form>

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <h2 className="text-sm font-semibold">검사 결과 <span className="ml-1 text-slate-400">{filtered.length.toLocaleString("ko-KR")}개</span></h2>
          <p className="text-xs text-slate-400">페이지당 {PAGE_SIZE}개</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead className="bg-slate-50 text-xs text-slate-500 dark:bg-slate-800/60 dark:text-slate-400">
              <tr>
                <th className="px-4 py-3 text-left font-semibold">상태</th>
                <th className="px-4 py-3 text-left font-semibold">사이트</th>
                <th className="px-4 py-3 text-left font-semibold">site_id</th>
                <th className="px-4 py-3 text-right font-semibold">과거 수집</th>
                <th className="px-4 py-3 text-left font-semibold">진단</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {visible.map((row) => (
                <tr className={row.status === "unhealthy" ? "bg-red-50/45 dark:bg-red-950/10" : "hover:bg-emerald-50/40 dark:hover:bg-emerald-950/10"} key={row.siteId}>
                  <td className="px-4 py-3 align-top"><StatusBadge row={row} /></td>
                  <td className="max-w-64 px-4 py-3 align-top font-medium">{row.siteName || row.siteId}</td>
                  <td className="px-4 py-3 align-top font-mono text-xs text-slate-500 dark:text-slate-400">{row.siteId}</td>
                  <td className="px-4 py-3 text-right align-top font-mono tabular-nums">{row.collected.toLocaleString("ko-KR")}</td>
                  <td className="max-w-sm px-4 py-3 align-top">
                    {row.category ? <span className="mb-1 inline-block rounded bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-950 dark:text-red-300">{row.category}</span> : <span className="text-emerald-700 dark:text-emerald-400">문서 저장 성공</span>}
                    {row.reason && <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">{row.reason}</p>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {visible.length === 0 && <div className="px-6 py-14 text-center text-sm text-slate-400">조건에 맞는 크롤러가 없습니다.</div>}
      </section>

      <nav className="flex items-center justify-between" aria-label="페이지 이동">
        {page > 1 ? <Link className="rounded-lg border border-slate-300 px-4 py-2 text-sm hover:bg-white dark:border-slate-700 dark:hover:bg-slate-900" href={hrefFor(current, page - 1)}>← 이전</Link> : <span />}
        <span className="text-xs font-mono text-slate-500">{page} / {pageCount}</span>
        {page < pageCount ? <Link className="rounded-lg border border-slate-300 px-4 py-2 text-sm hover:bg-white dark:border-slate-700 dark:hover:bg-slate-900" href={hrefFor(current, page + 1)}>다음 →</Link> : <span />}
      </nav>
    </div>
  );
}

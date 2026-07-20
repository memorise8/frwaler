import Link from "next/link";
import {
  getDocumentsBySummaryStatus,
  getSiteOptions,
  type LibertreeDocument,
} from "@/lib/db";
import { RegenerateButton } from "./_components/regenerate-button";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 30;

interface Props {
  searchParams: Promise<{
    status?: string;
    site?: string;
    page?: string;
  }>;
}

function parseStatus(s: string | undefined): "all" | "summarized" | "pending" {
  if (s === "summarized") return "summarized";
  if (s === "pending") return "pending";
  return "all";
}

function parsePage(p: string | undefined): number {
  const n = p ? Number.parseInt(p, 10) : 1;
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function trim(s: string | null, n: number): string {
  if (!s) return "";
  s = s.trim();
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

export default async function AdminSummaryPage({ searchParams }: Props) {
  const sp = await searchParams;
  const status = parseStatus(sp.status);
  const siteId = sp.site?.trim() || undefined;
  const page = parsePage(sp.page);
  const offset = (page - 1) * PAGE_SIZE;

  const hasSummary =
    status === "summarized" ? true : status === "pending" ? false : undefined;

  let result;
  let sites;
  let dbError: string | null = null;
  try {
    result = getDocumentsBySummaryStatus({
      hasSummary,
      siteId,
      limit: PAGE_SIZE,
      offset,
    });
    sites = getSiteOptions();
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  if (dbError || !result || !sites) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">요약 결과</h1>
        <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-6 text-red-700 dark:text-red-300">
          <p className="font-semibold">데이터베이스 연결 실패</p>
          <p className="text-sm mt-2">{dbError}</p>
        </div>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(result.total / PAGE_SIZE));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">요약 결과</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          AI 요약 (`documents.summary`) 의 생성 상태를 확인하고, 필요한 행을 재요약합니다.
        </p>
      </div>

      <form
        method="get"
        className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-4 flex flex-wrap items-end gap-3"
      >
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
            상태
          </label>
          <select
            name="status"
            defaultValue={status}
            className="px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 rounded-lg text-sm focus:ring-2 focus:ring-blue-500"
          >
            <option value="all">전체</option>
            <option value="summarized">요약 완료</option>
            <option value="pending">미요약</option>
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
            사이트
          </label>
          <select
            name="site"
            defaultValue={siteId ?? ""}
            className="px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 min-w-[12rem]"
          >
            <option value="">전체 사이트</option>
            {sites.map((s) => (
              <option key={s.site_id} value={s.site_id}>
                {s.site_name} ({s.site_id})
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          적용
        </button>
        <Link
          href="/admin/summary"
          className="px-4 py-2 border border-slate-300 dark:border-slate-700 rounded-lg text-sm hover:bg-slate-50 dark:hover:bg-slate-800"
        >
          초기화
        </Link>
        <span className="ml-auto text-xs text-slate-500 dark:text-slate-400">
          전체 {result.total.toLocaleString("ko-KR")}건 · 페이지 {page}/{totalPages}
        </span>
      </form>

      <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
        {result.documents.length === 0 ? (
          <div className="px-4 py-12 text-center text-slate-400 dark:text-slate-500">
            조건에 맞는 문서가 없습니다.
          </div>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {result.documents.map((doc) => (
              <SummaryRow key={doc.id} doc={doc} />
            ))}
          </ul>
        )}
      </div>

      <Pagination
        page={page}
        totalPages={totalPages}
        status={status}
        siteId={siteId}
      />
    </div>
  );
}

function SummaryRow({ doc }: { doc: LibertreeDocument }) {
  const hasSummary = !!doc.summary?.trim();
  return (
    <li className="px-5 py-4 hover:bg-slate-50 dark:hover:bg-slate-800/50">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2 flex-wrap">
            <Link
              href={`/search/${doc.id}`}
              className="font-semibold text-slate-900 dark:text-slate-100 hover:text-blue-700 dark:hover:text-blue-300"
            >
              {doc.title || "(제목 없음)"}
            </Link>
            <span className="font-mono text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
              #{doc.id}
            </span>
            <span className="text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
              {doc.site_id}
            </span>
            {hasSummary ? (
              <span className="text-xs px-2 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 font-medium">
                요약 완료
              </span>
            ) : (
              <span className="text-xs px-2 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-medium">
                미요약
              </span>
            )}
          </div>
          {hasSummary && (
            <p className="mt-2 text-sm text-slate-700 dark:text-slate-300 leading-relaxed whitespace-pre-wrap">
              {doc.summary}
            </p>
          )}
          {!hasSummary && doc.abstract && (
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400 leading-relaxed">
              <span className="font-medium text-slate-600 dark:text-slate-400">초록: </span>
              {trim(doc.abstract, 240)}
            </p>
          )}
        </div>
        <div className="shrink-0">
          <RegenerateButton docId={doc.id} />
        </div>
      </div>
    </li>
  );
}

function Pagination({
  page,
  totalPages,
  status,
  siteId,
}: {
  page: number;
  totalPages: number;
  status: string;
  siteId: string | undefined;
}) {
  if (totalPages <= 1) return null;
  const buildHref = (p: number) => {
    const params = new URLSearchParams();
    if (status !== "all") params.set("status", status);
    if (siteId) params.set("site", siteId);
    if (p > 1) params.set("page", String(p));
    const qs = params.toString();
    return qs ? `/admin/summary?${qs}` : "/admin/summary";
  };

  const prev = Math.max(1, page - 1);
  const next = Math.min(totalPages, page + 1);

  return (
    <div className="flex items-center justify-center gap-3 text-sm">
      <Link
        href={buildHref(prev)}
        aria-disabled={page <= 1}
        className={`px-3 py-1.5 border rounded-lg ${
          page <= 1
            ? "pointer-events-none opacity-40 border-slate-200 dark:border-slate-800"
            : "border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800"
        }`}
      >
        ← 이전
      </Link>
      <span className="text-xs text-slate-500 dark:text-slate-400 tabular-nums font-mono">
        {page} / {totalPages}
      </span>
      <Link
        href={buildHref(next)}
        aria-disabled={page >= totalPages}
        className={`px-3 py-1.5 border rounded-lg ${
          page >= totalPages
            ? "pointer-events-none opacity-40 border-slate-200 dark:border-slate-800"
            : "border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800"
        }`}
      >
        다음 →
      </Link>
    </div>
  );
}

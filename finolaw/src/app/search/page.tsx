import Link from "next/link";
import {
  searchPapers,
  getDocTypeCounts,
  getCategories,
  getDbState,
  parseMetadata,
  NTS_SITES,
} from "@/lib/db";

export const dynamic = "force-dynamic";

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

interface PageProps {
  searchParams: Promise<{
    q?: string;
    docType?: string;
    category?: string;
    site?: string;
    dateFrom?: string;
    dateTo?: string;
    page?: string;
  }>;
}

export default async function SearchPage({ searchParams }: PageProps) {
  const sp = await searchParams;
  const dbState = getDbState(["papers"]);
  const page = Math.max(1, parseInt(sp.page ?? "1", 10));
  const pageSize = 20;
  const q = sp.q?.trim() || undefined;
  const docType = sp.docType || undefined;
  const category = sp.category || undefined;
  const siteIds = sp.site ? [sp.site] : [...NTS_SITES];

  const [result, docTypes, categories] = await Promise.all([
    Promise.resolve(
      searchPapers({
        siteIds,
        q,
        docType,
        category,
        dateFrom: sp.dateFrom,
        dateTo: sp.dateTo,
        page,
        pageSize,
      })
    ),
    Promise.resolve(getDocTypeCounts()),
    Promise.resolve(getCategories()),
  ]);

  const totalPages = Math.max(1, Math.ceil(result.total / pageSize));

  const inputClass =
    "px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">세법 검색</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          국세법령 판례·해석례 통합 검색
        </p>
      </div>

      {dbState.kind !== "ready" && (
        <div className="rounded-xl border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 p-5 text-amber-900 dark:text-amber-200">
          <p className="font-semibold">아직 수집된 데이터가 없습니다.</p>
          <p className="text-sm mt-2">
            크롤링을 시작하면 검색 결과와 필터 목록이 여기에 표시됩니다.
          </p>
          <p className="text-xs mt-3 font-mono opacity-75">
            DB 경로: {dbState.path}
          </p>
          {dbState.kind === "incomplete" && dbState.missingTables && (
            <p className="text-xs mt-1 opacity-75">
              준비 중인 테이블: {dbState.missingTables.join(", ")}
            </p>
          )}
        </div>
      )}

      <form className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4">
        <div>
          <input
            type="text"
            name="q"
            defaultValue={q ?? ""}
            placeholder="제목 · 본문 · 메타데이터 검색"
            className={`w-full ${inputClass} py-2.5 text-sm`}
          />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <select name="site" defaultValue={sp.site ?? ""} className={inputClass}>
            <option value="">전체 사이트</option>
            <option value="nts-taxlaw-pd">판례 (pd)</option>
            <option value="nts-taxlaw-qt">해석례 (qt)</option>
          </select>
          <select
            name="docType"
            defaultValue={docType ?? ""}
            className={inputClass}
          >
            <option value="">전체 유형</option>
            {docTypes.map((d) => (
              <option key={d.documentTypeName} value={d.documentTypeName}>
                {d.documentTypeName} ({formatNum(d.count)})
              </option>
            ))}
          </select>
          <select
            name="category"
            defaultValue={category ?? ""}
            className={inputClass}
          >
            <option value="">전체 세목</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <input
            type="text"
            name="dateFrom"
            defaultValue={sp.dateFrom ?? ""}
            placeholder="부터 (YYYY-MM-DD)"
            className={inputClass}
          />
          <input
            type="text"
            name="dateTo"
            defaultValue={sp.dateTo ?? ""}
            placeholder="까지 (YYYY-MM-DD)"
            className={inputClass}
          />
        </div>
        <div className="flex items-center justify-between">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            결과 <span className="font-semibold">{formatNum(result.total)}</span>
            건 · {page} / {totalPages} 페이지
          </div>
          <div className="flex gap-2">
            <Link
              href="/search"
              className="px-4 py-2 border border-slate-300 dark:border-slate-700 rounded-lg text-sm hover:bg-slate-50 dark:hover:bg-slate-800"
            >
              초기화
            </Link>
            <button
              type="submit"
              className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium"
            >
              검색
            </button>
          </div>
        </div>
      </form>

      <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
        {result.papers.length === 0 ? (
          <div className="px-6 py-16 text-center text-slate-400 dark:text-slate-500 text-sm">
            {dbState.kind === "ready"
              ? "검색 결과가 없습니다."
              : "아직 수집된 데이터가 없습니다. 크롤링을 시작하면 여기에 결과가 표시됩니다."}
          </div>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {result.papers.map((p) => {
              const md = parseMetadata(p.metadata);
              return (
                <li
                  key={p.id}
                  className="px-5 py-4 hover:bg-slate-50 dark:hover:bg-slate-800/50"
                >
                  <Link href={`/search/${p.id}`} className="block">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <h3 className="font-semibold text-slate-900 dark:text-slate-100 truncate">
                          {p.title || "(제목 없음)"}
                        </h3>
                        <div className="flex items-center gap-2 mt-1 text-xs text-slate-500 dark:text-slate-400 flex-wrap">
                          {md.documentTypeName && (
                            <span className="px-2 py-0.5 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 rounded font-medium">
                              {md.documentTypeName}
                            </span>
                          )}
                          {p.category && (
                            <span className="px-2 py-0.5 bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 rounded font-medium">
                              {p.category}
                            </span>
                          )}
                          {md.documentNumber && (
                            <span className="font-mono">
                              {md.documentNumber}
                            </span>
                          )}
                          {p.published_date && (
                            <span>· {p.published_date}</span>
                          )}
                          <span className="text-slate-400 dark:text-slate-500">
                            · {p.site_id}
                          </span>
                        </div>
                      </div>
                      <span className="text-slate-300 dark:text-slate-600 text-lg">
                        →
                      </span>
                    </div>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <PageLink
            sp={sp}
            page={Math.max(1, page - 1)}
            disabled={page <= 1}
            label="← 이전"
          />
          <span className="text-sm text-slate-500 dark:text-slate-400 px-4">
            {page} / {totalPages}
          </span>
          <PageLink
            sp={sp}
            page={Math.min(totalPages, page + 1)}
            disabled={page >= totalPages}
            label="다음 →"
          />
        </div>
      )}
    </div>
  );
}

function PageLink({
  sp,
  page,
  disabled,
  label,
}: {
  sp: Record<string, string | undefined>;
  page: number;
  disabled: boolean;
  label: string;
}) {
  if (disabled) {
    return (
      <span className="px-4 py-2 rounded-lg text-sm border border-slate-200 dark:border-slate-800 text-slate-300 dark:text-slate-600 cursor-not-allowed">
        {label}
      </span>
    );
  }
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(sp)) {
    if (v) params.set(k, v);
  }
  params.set("page", String(page));
  return (
    <Link
      href={`/search?${params.toString()}`}
      className="px-4 py-2 rounded-lg text-sm border border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800"
    >
      {label}
    </Link>
  );
}

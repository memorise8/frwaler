import Link from "next/link";
import {
  getDbState,
  getSiteOptionsRich,
  searchInternalReviewDocuments,
  type SiteOptionRich,
} from "@/lib/db";
import {
  ALL_CATEGORIES,
  ALL_CONTINENTS,
  type Continent,
  type SiteFunctionCategory,
} from "@/lib/categories";

export const dynamic = "force-dynamic";

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

interface PageProps {
  searchParams: Promise<{
    q?: string;
    site?: string;
    sheet?: string;
    continent?: string;
    country?: string;
    category?: string;
    dateFrom?: string;
    dateTo?: string;
    page?: string;
  }>;
}

function uniqueSorted<T extends string>(values: Iterable<T>): T[] {
  return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b));
}

function isContinent(s: string | undefined): s is Continent {
  return !!s && (ALL_CONTINENTS as readonly string[]).includes(s);
}

function isCategory(s: string | undefined): s is SiteFunctionCategory {
  return !!s && (ALL_CATEGORIES as readonly string[]).includes(s);
}

function applySiteFilters(
  sites: SiteOptionRich[],
  filters: {
    continent?: Continent;
    country?: string;
    category?: SiteFunctionCategory;
    sheet?: string;
    siteId?: string;
  },
): SiteOptionRich[] {
  return sites.filter((s) => {
    if (filters.siteId && s.site_id !== filters.siteId) return false;
    if (filters.continent && s.continent !== filters.continent) return false;
    if (filters.country && s.country !== filters.country) return false;
    if (filters.category && s.category !== filters.category) return false;
    if (filters.sheet && (s.sheet ?? "(없음)") !== filters.sheet) return false;
    return true;
  });
}

export default async function SearchPage({ searchParams }: PageProps) {
  const sp = await searchParams;
  const dbState = getDbState(["documents"]);
  const requestedPage = Number.parseInt(sp.page ?? "1", 10);
  const page = Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const pageSize = 20;
  const q = sp.q?.trim() || undefined;
  const sites = getSiteOptionsRich();

  const continent = isContinent(sp.continent) ? sp.continent : undefined;
  const category = isCategory(sp.category) ? sp.category : undefined;
  const country = sp.country?.trim() || undefined;
  const sheet = sp.sheet?.trim() || undefined;
  const siteId = sp.site?.trim() || undefined;

  // Sites that match the structural filters (continent/country/category/sheet).
  // The site dropdown narrows progressively, and the search itself targets
  // these site IDs unless the user explicitly picks one site.
  const filteredSites = applySiteFilters(sites, { continent, country, category, sheet });
  const candidateSiteIds = (
    siteId ? filteredSites.filter((s) => s.site_id === siteId) : filteredSites
  ).map((s) => s.site_id);

  // Build dropdown option lists.
  const continentOptions = uniqueSorted(sites.map((s) => s.continent));
  // For country/sheet/category dropdowns, narrow by the continent so the
  // user sees only meaningful sub-options.
  const sitesForCountryList = continent
    ? sites.filter((s) => s.continent === continent)
    : sites;
  const countryOptions = uniqueSorted(sitesForCountryList.map((s) => s.country));
  const categoryOptions = uniqueSorted(sitesForCountryList.map((s) => s.category));
  const sheetOptions = uniqueSorted(
    sitesForCountryList.map((s) => s.sheet ?? "(없음)"),
  );

  // If a structural filter eliminates everything, return empty result quickly.
  const result = candidateSiteIds.length === 0
    ? { items: [], total: 0, page, pageSize }
    : searchInternalReviewDocuments({
        siteIds: candidateSiteIds,
        q,
        dateFrom: sp.dateFrom,
        dateTo: sp.dateTo,
        page,
        pageSize,
      });

  const totalPages = Math.max(1, Math.ceil(result.total / pageSize));

  const inputClass =
    "w-full min-w-0 max-w-full px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent";

  return (
    <div className="min-w-0 max-w-full space-y-6">
      <div>
        <h1 className="text-2xl font-bold">문서 검색</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          전세계 정부·연구·학술 사이트 통합 검색
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

      <form className="min-w-0 max-w-full bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4">
        <div>
          <input
            type="text"
            name="q"
            defaultValue={q ?? ""}
            placeholder="제목 · 본문 · 키워드 · 저자 검색"
            className={`w-full ${inputClass} py-2.5 text-sm`}
          />
        </div>

        <div className="grid min-w-0 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3">
          <FilterSelect name="continent" label="대륙" value={continent ?? ""} options={continentOptions} inputClass={inputClass} placeholder="전체 대륙" />
          <FilterSelect name="country" label="국가" value={country ?? ""} options={countryOptions} inputClass={inputClass} placeholder="전체 국가" />
          <FilterSelect name="category" label="범주" value={category ?? ""} options={categoryOptions} inputClass={inputClass} placeholder="전체 범주" />
          <FilterSelect name="sheet" label="Sheet" value={sheet ?? ""} options={sheetOptions} inputClass={inputClass} placeholder="전체 sheet" />
        </div>

        <div className="grid min-w-0 grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
          <div className="md:col-span-1">
            <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
              사이트 (선택)
            </label>
            <select name="site" defaultValue={siteId ?? ""} className={`w-full ${inputClass}`}>
              <option value="">전체 사이트 ({filteredSites.length})</option>
              {filteredSites.map((s) => (
                <option key={s.site_id} value={s.site_id}>
                  {s.site_name} · {s.country} ({formatNum(s.docs)})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
              발행일 (YYYY-MM-DD)
            </label>
            <input
              type="text"
              name="dateFrom"
              defaultValue={sp.dateFrom ?? ""}
              placeholder="부터"
              className={`w-full ${inputClass}`}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
              &nbsp;
            </label>
            <input
              type="text"
              name="dateTo"
              defaultValue={sp.dateTo ?? ""}
              placeholder="까지"
              className={`w-full ${inputClass}`}
            />
          </div>
        </div>

        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            결과 <span className="font-semibold">{formatNum(result.total)}</span>건 · 사이트
            {" "}<span className="font-semibold">{formatNum(candidateSiteIds.length)}</span>개 · {page} / {totalPages} 페이지
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
        {result.items.length === 0 ? (
          <div className="px-6 py-16 text-center text-slate-400 dark:text-slate-500 text-sm">
            {dbState.kind === "ready"
              ? "검색 결과가 없습니다."
              : "아직 수집된 데이터가 없습니다. 크롤링을 시작하면 여기에 결과가 표시됩니다."}
          </div>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {result.items.map((item) => {
              return (
                <li
                  key={item.id}
                  className="px-5 py-4 hover:bg-slate-50 dark:hover:bg-slate-800/50"
                >
                  <Link href={`/search/${item.id}`} className="block">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <h3 className="font-semibold text-slate-900 dark:text-slate-100 break-keep leading-snug">
                          {item.title}
                        </h3>
                        <div className="flex items-center gap-2 mt-1 text-xs text-slate-500 dark:text-slate-400 flex-wrap">
                          <span className="px-2 py-0.5 bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 rounded font-medium">
                            {item.source.country}
                          </span>
                          <span className="px-2 py-0.5 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 rounded font-medium">
                            {item.source.category}
                          </span>
                          {item.publishedDate && (
                            <span>· {item.publishedDate}</span>
                          )}
                          <span className="text-slate-400 dark:text-slate-500">
                            · {item.source.name}
                          </span>
                        </div>
                        {item.description.text ? (
                          <p className="mt-2 text-sm text-slate-600 dark:text-slate-300 line-clamp-2 leading-relaxed">
                            <span className="mr-1 text-xs text-slate-400 dark:text-slate-500">
                              {item.description.provenance === "summary" ? "소개·요약" : "소개·초록"}
                            </span>
                            {item.description.text}
                          </p>
                        ) : (
                          <p className="mt-2 text-sm text-slate-400 dark:text-slate-500">
                            소개 정보가 아직 없습니다.
                          </p>
                        )}
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

function FilterSelect({
  name,
  label,
  value,
  options,
  inputClass,
  placeholder,
}: {
  name: string;
  label: string;
  value: string;
  options: string[];
  inputClass: string;
  placeholder: string;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
        {label}
      </label>
      <select name={name} defaultValue={value} className={`w-full ${inputClass}`}>
        <option value="">{placeholder}</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
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

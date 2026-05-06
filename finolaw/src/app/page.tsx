import Link from "next/link";
import {
  getDbStats,
  getDocTypeCounts,
  getAllSitesSummary,
  getRecentPapers,
  parseMetadata,
} from "@/lib/db";
import { getPreflightStatus, type PreflightCheck, type PreflightLevel } from "@/lib/preflight";

export const dynamic = "force-dynamic";

const DOC_TYPE_TARGETS: Record<string, number> = {
  판례: 54627,
  심판: 70681,
  질의: 132454,
  심사: 22203,
  이의: 1470,
  헌재: 355,
  적부: 501,
  기준: 1011,
  고시: 13,
  사전: 4945,
};

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

export default async function DashboardPage() {
  const preflight = getPreflightStatus();
  let stats, docTypes, sites, recentPapers;
  let dbError: string | null = null;
  try {
    stats = getDbStats();
    docTypes = getDocTypeCounts();
    sites = getAllSitesSummary();
    recentPapers = getRecentPapers(undefined, 20);
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  if (dbError || !stats || !docTypes || !sites || !recentPapers) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">대시보드</h1>
        <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-6 text-red-700 dark:text-red-300">
          <p className="font-semibold">데이터베이스 연결 실패</p>
          <p className="text-sm mt-2">{dbError}</p>
          <p className="text-xs mt-3 font-mono opacity-75">
            예상 경로: ../data/data.db
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold">대시보드</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            수집 문서 현황과 시스템 상태를 한 화면에서 확인합니다.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="text-xs text-slate-400 dark:text-slate-500">
            마지막 크롤링: {formatDate(stats.lastCrawled)}
          </div>
          <Link
            href="/api/export/markdown"
            className="px-3 py-1.5 rounded-lg border border-slate-300 dark:border-slate-700 text-xs font-medium hover:bg-slate-50 dark:hover:bg-slate-800"
          >
            전체 MD export
          </Link>
        </div>
      </div>

      {preflight.status !== "ok" && (
        <PreflightBanner
          status={preflight.status}
          checks={preflight.checks.filter((check) => check.level !== "ok")}
        />
      )}

      {stats.totalPapers === 0 && (
        <div className="rounded-xl border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 p-5 text-amber-900 dark:text-amber-200">
          <p className="font-semibold">아직 수집된 데이터가 없습니다.</p>
          <p className="text-sm mt-2">
            크롤링을 시작하면 대시보드 수치와 검색 결과가 자동으로 채워집니다.
          </p>
        </div>
      )}

      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="전체 문서" value={formatNum(stats.totalPapers)} tone="indigo" />
        <Stat
          label="커스텀 사이트 문서"
          value={formatNum(stats.customPapers)}
          tone="blue"
        />
        <Stat
          label="활성 사이트"
          value={formatNum(stats.totalSites)}
          tone="emerald"
        />
        <Stat label="등록된 사이트 메타" value={String(stats.registeredSites)} tone="amber" />
      </section>

      <section className="grid grid-cols-1 md:grid-cols-5 gap-4">
        <QuickLink
          href="/search"
          title="세법 검색"
          desc={`${formatNum(stats.totalPapers)}건의 수집 문서 전체 검색`}
          icon="🔍"
          color="bg-blue-50 dark:bg-blue-950/30 border-blue-200 dark:border-blue-900 hover:bg-blue-100 dark:hover:bg-blue-950/50"
        />
        <QuickLink
          href="/smart-find"
          title="URL 크롤링"
          desc="URL만 입력하면 자동으로 문서를 발견 (GPT/Gemini)"
          icon="🌐"
          color="bg-purple-50 dark:bg-purple-950/30 border-purple-200 dark:border-purple-900 hover:bg-purple-100 dark:hover:bg-purple-950/50"
        />
        <QuickLink
          href="/auto-add"
          title="Auto-Add"
          desc="URL 하나로 GPT 에이전트가 크롤러 config를 자동 생성"
          icon="🧠"
          color="bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-900 hover:bg-amber-100 dark:hover:bg-amber-950/50"
        />
        <QuickLink
          href="/crawler"
          title="크롤러 관리"
          desc="증분/수동 실행 및 로그 스트리밍"
          icon="⚙️"
          color="bg-slate-50 dark:bg-slate-800/50 border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800"
        />
        <QuickLink
          href="/products/import"
          title="제품 Import"
          desc="페이지별 CSV를 한 번에 합쳐 제품 DB 저장"
          icon="📦"
          color="bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-900 hover:bg-emerald-100 dark:hover:bg-emerald-950/50"
        />
      </section>

      <section>
        <div className="flex items-center justify-between gap-3 mb-3">
          <div>
            <h2 className="text-lg font-semibold">최근 수집 문서</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
              최근 크롤링된 문서 20건을 바로 확인하고 상세 페이지로 이동할 수 있습니다.
            </p>
          </div>
          <Link
            href="/api/export/markdown"
            className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
          >
            Markdown으로 저장
          </Link>
        </div>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          {recentPapers.length === 0 ? (
            <div className="px-4 py-10 text-center text-slate-400 dark:text-slate-500">
              아직 표시할 문서가 없습니다.
            </div>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {recentPapers.map((paper) => {
                const md = parseMetadata(paper.metadata);
                return (
                  <li key={paper.id} className="px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <Link
                          href={`/search/${paper.id}`}
                          className="font-medium text-slate-900 dark:text-slate-100 hover:text-blue-700 dark:hover:text-blue-300"
                        >
                          {paper.title || "(제목 없음)"}
                        </Link>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                          <span className="font-mono">{paper.site_id}</span>
                          {md.documentTypeName && <span>· {md.documentTypeName}</span>}
                          {paper.category && <span>· {paper.category}</span>}
                          {paper.published_date && <span>· {paper.published_date}</span>}
                        </div>
                      </div>
                      <div className="shrink-0 text-right">
                        <div className="text-xs text-slate-400 dark:text-slate-500">
                          {formatDate(paper.crawled_at)}
                        </div>
                        <div className="mt-1">
                          <Link
                            href={`/api/export/markdown?site=${encodeURIComponent(paper.site_id)}&limit=50`}
                            className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                          >
                            이 사이트 MD
                          </Link>
                        </div>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">문서 유형별 수집 현황</h2>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
              <tr>
                <th className="text-left px-4 py-3 font-medium">유형</th>
                <th className="text-right px-4 py-3 font-medium">수집</th>
                <th className="text-right px-4 py-3 font-medium">목표</th>
                <th className="text-left px-4 py-3 font-medium">진행률</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {docTypes.length === 0 && (
                <tr>
                  <td
                    colSpan={4}
                    className="px-4 py-10 text-center text-slate-400 dark:text-slate-500"
                  >
                    아직 집계할 문서가 없습니다.
                  </td>
                </tr>
              )}
              {docTypes.map((d) => {
                const target = DOC_TYPE_TARGETS[d.documentTypeName] ?? d.count;
                const pct = Math.min(100, Math.round((d.count / target) * 100));
                return (
                  <tr
                    key={d.documentTypeName}
                    className="hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  >
                    <td className="px-4 py-2.5 font-medium">
                      {d.documentTypeName}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono">
                      {formatNum(d.count)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-slate-500 dark:text-slate-400">
                      {formatNum(target)}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-2 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full ${
                              pct >= 99
                                ? "bg-emerald-500"
                                : pct >= 90
                                  ? "bg-blue-500"
                                  : "bg-amber-500"
                            }`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span className="text-xs text-slate-500 dark:text-slate-400 w-10 text-right tabular-nums">
                          {pct}%
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">등록된 사이트 ({sites.length})</h2>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
              <tr>
                <th className="text-left px-4 py-3 font-medium">site_id</th>
                <th className="text-left px-4 py-3 font-medium">이름</th>
                <th className="text-right px-4 py-3 font-medium">문서 수</th>
                <th className="text-right px-4 py-3 font-medium">최근 수집</th>
                <th className="text-right px-4 py-3 font-medium">작업</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {sites.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-10 text-center text-slate-400 dark:text-slate-500"
                  >
                    아직 표시할 사이트 요약이 없습니다.
                  </td>
                </tr>
              )}
              {sites.slice(0, 20).map((s) => (
                <tr
                  key={s.site_id}
                  className="hover:bg-slate-50 dark:hover:bg-slate-800/50"
                >
                  <td className="px-4 py-2 font-mono text-xs text-slate-600 dark:text-slate-400">
                    {s.site_id}
                  </td>
                  <td className="px-4 py-2">{s.site_name}</td>
                  <td className="px-4 py-2 text-right font-mono">
                    {formatNum(s.count)}
                  </td>
                  <td className="px-4 py-2 text-right text-xs text-slate-500 dark:text-slate-400">
                    {formatDate(s.last_crawled)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="inline-flex items-center gap-3 text-xs">
                      <Link
                        href={`/search?site=${encodeURIComponent(s.site_id)}`}
                        className="text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100"
                      >
                        보기
                      </Link>
                      <Link
                        href={`/api/export/markdown?site=${encodeURIComponent(s.site_id)}&limit=200`}
                        className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                      >
                        MD export
                      </Link>
                    </div>
                  </td>
                </tr>
              ))}
              {sites.length > 20 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-2 text-center text-xs text-slate-400 dark:text-slate-500"
                  >
                    ...외 {sites.length - 20}개
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function PreflightBanner({
  status,
  checks,
}: {
  status: PreflightLevel;
  checks: PreflightCheck[];
}) {
  const isError = status === "error";
  const boxClass = isError
    ? "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-200"
    : "border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 text-amber-900 dark:text-amber-200";

  return (
    <section className={`rounded-xl border p-5 ${boxClass}`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-semibold">
            {isError ? "시스템 필수 조건 확인 필요" : "운영 전 확인할 항목이 있습니다"}
          </p>
          <p className="text-sm mt-1 opacity-85">
            DB, Python, Codex, API 키, 관리자 인증 상태를 점검했습니다.
          </p>
        </div>
        <Link
          href="/api/health"
          className="shrink-0 text-xs underline underline-offset-4 opacity-80 hover:opacity-100"
        >
          JSON 보기
        </Link>
      </div>
      <ul className="mt-3 space-y-1.5 text-sm">
        {checks.map((check) => (
          <li key={check.id} className="flex gap-2">
            <span aria-hidden="true">{check.level === "error" ? "❌" : "⚠️"}</span>
            <span>
              <strong>{check.label}:</strong> {check.message}
              {check.detail && (
                <span className="block text-xs font-mono opacity-70">{check.detail}</span>
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
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

function QuickLink({
  href,
  title,
  desc,
  icon,
  color,
}: {
  href: string;
  title: string;
  desc: string;
  icon: string;
  color: string;
}) {
  return (
    <Link
      href={href}
      className={`block rounded-xl border p-5 transition-colors ${color}`}
    >
      <div className="flex items-start gap-3">
        <span className="text-3xl">{icon}</span>
        <div>
          <h3 className="font-semibold">{title}</h3>
          <p className="text-sm text-slate-600 dark:text-slate-300 mt-1">
            {desc}
          </p>
        </div>
      </div>
    </Link>
  );
}

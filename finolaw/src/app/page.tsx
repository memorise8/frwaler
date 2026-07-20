import Link from "next/link";
import {
  getDashboardStats,
  getCollectionReport,
  type DashboardGroupRow,
  type CollectionReport,
} from "@/lib/db";
import { getPreflightStatus, type PreflightCheck, type PreflightLevel } from "@/lib/preflight";

export const dynamic = "force-dynamic";

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

function pct(part: number, whole: number): number {
  if (!whole) return 0;
  return Math.min(100, Math.round((part / whole) * 100));
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
  let dashboard: ReturnType<typeof getDashboardStats> | null = null;
  let dbError: string | null = null;
  try {
    dashboard = getDashboardStats();
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  if (dbError || !dashboard) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Libertree — 글로벌 문서 허브</h1>
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

  const { kpi, byContinent, byCountry, byCategory, recentSites, classification } = dashboard;
  const top10Countries = byCountry.slice(0, 10);

  let report: CollectionReport | null = null;
  try {
    report = getCollectionReport();
  } catch {
    report = null;
  }

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold">Libertree — 글로벌 문서 허브</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            전세계 정부·연구·학술 사이트의 문서 통합 검색 및 수집 현황
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="text-xs text-slate-400 dark:text-slate-500">
            마지막 수집: {formatDate(kpi.lastCrawled)}
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

      {kpi.totalDocs === 0 && (
        <div className="rounded-xl border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 p-5 text-amber-900 dark:text-amber-200">
          <p className="font-semibold">아직 수집된 문서가 없습니다.</p>
          <p className="text-sm mt-2">
            크롤링을 시작하면 대시보드 수치와 검색 결과가 자동으로 채워집니다.
          </p>
        </div>
      )}

      {/* ===================== KPI cards ===================== */}
      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat
          label="총 문서 수"
          value={formatNum(kpi.totalDocs)}
          subtitle={`최근 ${formatDate(kpi.lastCrawled)}`}
          tone="indigo"
        />
        <Stat
          label="총 사이트 수"
          value={formatNum(kpi.totalSites)}
          subtitle={`등록 메타 ${formatNum(kpi.registeredSites)}건`}
          tone="emerald"
        />
        <Stat
          label="PDF 보유"
          value={`${formatNum(kpi.pdfDocs)}`}
          subtitle={`전체 대비 ${pct(kpi.pdfDocs, kpi.totalDocs)}%`}
          tone="blue"
        />
        <Stat
          label="TXT 추출"
          value={`${formatNum(kpi.txtDocs)}`}
          subtitle={`전체 대비 ${pct(kpi.txtDocs, kpi.totalDocs)}%`}
          tone="amber"
        />
      </section>

      {/* ===================== Input → Collection funnel ===================== */}
      {report && <CollectionFunnel report={report} />}

      {/* ===================== Continent breakdown ===================== */}
      <section>
        <div className="flex items-end justify-between mb-3">
          <div>
            <h2 className="text-lg font-semibold">대륙별 분포</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              사이트는 sheet 라벨을 기반으로 자동 분류됩니다.
            </p>
          </div>
          <span className="text-xs text-slate-400 dark:text-slate-500">
            매핑 {classification.mappedSheets} / {classification.totalSheets}
            {classification.unmappedSheets > 0 && (
              <> · 미분류 {classification.unmappedSheets}</>
            )}
          </span>
        </div>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          {byContinent.length === 0 ? (
            <EmptyHint />
          ) : (
            <GroupTable rows={byContinent} totalDocs={kpi.totalDocs} headerLabel="대륙" />
          )}
        </div>
      </section>

      {/* ===================== Country & Category side-by-side ===================== */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <h2 className="text-lg font-semibold mb-3">국가별 상위 10개</h2>
          <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
            {top10Countries.length === 0 ? (
              <EmptyHint />
            ) : (
              <GroupTable rows={top10Countries} totalDocs={kpi.totalDocs} headerLabel="국가" />
            )}
          </div>
        </div>
        <div>
          <h2 className="text-lg font-semibold mb-3">범주별 분포</h2>
          <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
            {byCategory.length === 0 ? (
              <EmptyHint />
            ) : (
              <GroupTable rows={byCategory} totalDocs={kpi.totalDocs} headerLabel="범주" />
            )}
          </div>
        </div>
      </section>

      {/* ===================== Recent collection ===================== */}
      <section>
        <div className="flex items-end justify-between mb-3">
          <div>
            <h2 className="text-lg font-semibold">최근 수집 사이트</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              가장 최근에 수집된 사이트 8개. 각 사이트의 문서를 바로 확인할 수 있습니다.
            </p>
          </div>
          <Link
            href="/admin/status"
            className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
          >
            전체 수집 현황 →
          </Link>
        </div>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          {recentSites.length === 0 ? (
            <EmptyHint />
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
                <tr>
                  <th className="text-left px-4 py-3 font-medium">사이트</th>
                  <th className="text-left px-4 py-3 font-medium">국가 / 범주</th>
                  <th className="text-right px-4 py-3 font-medium">문서 수</th>
                  <th className="text-right px-4 py-3 font-medium">최근 수집</th>
                  <th className="text-right px-4 py-3 font-medium">바로가기</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {recentSites.map((s) => (
                  <tr key={s.site_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{s.site_name}</div>
                      <div className="font-mono text-xs text-slate-500 dark:text-slate-400">
                        {s.site_id}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-sm text-slate-600 dark:text-slate-300">
                      <span className="font-medium">{s.country}</span>
                      <span className="text-slate-400 dark:text-slate-500"> · {s.continent}</span>
                      <span className="ml-2 inline-flex items-center text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
                        {s.category}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono">{formatNum(s.docs)}</td>
                    <td className="px-4 py-2.5 text-right text-xs text-slate-500 dark:text-slate-400">
                      {formatDate(s.last_crawled)}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Link
                        href={`/search?site=${encodeURIComponent(s.site_id)}`}
                        className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
                      >
                        문서 보기
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* ===================== Quick links ===================== */}
      <section className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <QuickLink
          href="/search"
          title="문서 검색"
          desc={`${formatNum(kpi.totalDocs)}건의 수집 문서 통합 검색`}
          icon="🔍"
          color="bg-blue-50 dark:bg-blue-950/30 border-blue-200 dark:border-blue-900 hover:bg-blue-100 dark:hover:bg-blue-950/50"
        />
        <QuickLink
          href="/admin/status"
          title="수집 현황"
          desc="사이트별 수집 → PDF → TXT → 요약 진행률"
          icon="📈"
          color="bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-900 hover:bg-emerald-100 dark:hover:bg-emerald-950/50"
        />
        <QuickLink
          href="/smart-find"
          title="URL 크롤링"
          desc="URL만 입력하면 자동으로 문서를 발견 (GPT/Gemini)"
          icon="🛰️"
          color="bg-purple-50 dark:bg-purple-950/30 border-purple-200 dark:border-purple-900 hover:bg-purple-100 dark:hover:bg-purple-950/50"
        />
        <QuickLink
          href="/crawler"
          title="크롤러 관리"
          desc="증분/수동 실행 및 로그 스트리밍"
          icon="⚙️"
          color="bg-slate-50 dark:bg-slate-800/50 border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800"
        />
      </section>
    </div>
  );
}

const RECOVERY_TONES: { match: string; bar: string }[] = [
  { match: "자동회복", bar: "bg-indigo-500" },
  { match: "수집완료", bar: "bg-emerald-500" },
  { match: "회원가입", bar: "bg-amber-500" },
  { match: "정책", bar: "bg-orange-500" },
  { match: "절대불가", bar: "bg-rose-500" },
  { match: "미분류", bar: "bg-slate-400" },
];

function recoveryBarColor(category: string): string {
  return RECOVERY_TONES.find((t) => category.includes(t.match))?.bar ?? "bg-slate-400";
}

function CollectionFunnel({ report }: { report: CollectionReport }) {
  const {
    inputEntries,
    inputUniqueHosts,
    registeredCrawlers,
    collectedSites,
    recoveryCategories,
  } = report;

  // CSV가 없거나 집계 실패 시 섹션 자체를 숨김
  if (!inputEntries) return null;

  const funnel = [
    { label: "입력 entries", count: inputEntries, color: "bg-indigo-500" },
    { label: "unique hosts", count: inputUniqueHosts, color: "bg-indigo-400" },
    { label: "등록 크롤러", count: registeredCrawlers, color: "bg-blue-400" },
    { label: "수집 완료", count: collectedSites, color: "bg-emerald-500" },
  ];
  const maxCount = Math.max(...funnel.map((f) => f.count), 1);

  const recoveryTotal = recoveryCategories.reduce((acc, c) => acc + c.count, 0);
  const sumWhere = (needle: string) =>
    recoveryCategories
      .filter((c) => c.category.includes(needle))
      .reduce((acc, c) => acc + c.count, 0);
  const collectible = sumWhere("자동회복") + sumWhere("수집완료");
  const manual = sumWhere("회원가입");
  const impossible = sumWhere("정책") + sumWhere("절대불가");
  const unknown = sumWhere("미분류");

  return (
    <section className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold">입력 → 등록 → 수집 Funnel</h2>
        <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
          수집 대상 {formatNum(inputEntries)}개 입력 중 실제 적재까지의 단계별 현황입니다.
        </p>
      </div>

      {/* Part A — funnel bars */}
      <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-3">
        {funnel.map((f) => {
          const width = Math.round((f.count / maxCount) * 100);
          return (
            <div key={f.label} className="flex items-center gap-3">
              <div className="w-24 sm:w-28 shrink-0 text-xs text-slate-500 dark:text-slate-400 text-right">
                {f.label}
              </div>
              <div className="flex-1 h-7 bg-slate-100 dark:bg-slate-800 rounded-md overflow-hidden">
                <div
                  className={`h-full ${f.color} flex items-center justify-end px-2`}
                  style={{ width: `${Math.max(width, 8)}%` }}
                >
                  <span className="text-xs font-semibold text-white tabular-nums">
                    {formatNum(f.count)}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
        <div className="text-xs text-slate-500 dark:text-slate-400 pt-1">
          입력 대비 <strong>{pct(collectedSites, inputEntries)}%</strong> 수집 · 등록 크롤러 대비{" "}
          <strong>{pct(collectedSites, registeredCrawlers)}%</strong>
        </div>
      </div>

      {/* Part B + Part C */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Part B — recovery category breakdown */}
        <div>
          <h3 className="text-sm font-semibold mb-2">회복 카테고리 분류</h3>
          <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-4 space-y-2.5">
            {recoveryCategories.length === 0 ? (
              <EmptyHint />
            ) : (
              recoveryCategories.map((c) => {
                const width = pct(c.count, recoveryTotal);
                return (
                  <div key={c.category} className="flex items-center gap-3">
                    <div className="w-40 shrink-0 text-xs truncate" title={c.category}>
                      {c.category}
                    </div>
                    <div className="flex-1 h-4 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${recoveryBarColor(c.category)}`}
                        style={{ width: `${Math.max(width, 2)}%` }}
                      />
                    </div>
                    <div className="w-20 shrink-0 text-right text-xs font-mono tabular-nums">
                      {formatNum(c.count)}
                      <span className="text-slate-400 dark:text-slate-500"> {width}%</span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Part C — collectible / manual / impossible summary */}
        <div>
          <h3 className="text-sm font-semibold mb-2">되는 것 / 안 되는 것</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <SummaryCard
              tone="emerald"
              title="✅ 수집 가능"
              count={collectible}
              total={recoveryTotal}
              note="자동회복중 + 수집완료"
            />
            <SummaryCard
              tone="amber"
              title="🔐 수동 회복"
              count={manual}
              total={recoveryTotal}
              note="회원가입 시 가능"
            />
            <SummaryCard
              tone="rose"
              title="❌ 회복 불가"
              count={impossible}
              total={recoveryTotal}
              note="정책포기 + 절대불가"
            />
          </div>
          {unknown > 0 && (
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-3">
              ❓ 미분류 {formatNum(unknown)}건은 수동 점검 대상입니다.
            </p>
          )}
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-slate-500 dark:text-slate-400">
              카테고리 의미 설명
            </summary>
            <ul className="mt-2 text-xs space-y-1.5 text-slate-600 dark:text-slate-400 list-disc pl-4">
              <li><strong>자동회복중</strong> — 시스템 자동 배치 처리 중, 개입 불필요</li>
              <li><strong>수집완료</strong> — DB에 적재됨</li>
              <li><strong>회원가입 필요</strong> — 로그인/토큰 필요. 수동 회복 가능</li>
              <li><strong>정책상포기</strong> — robots.txt 거부. 우회하지 않음</li>
              <li><strong>절대불가</strong> — dead/영구차단. 회복 수단 없음</li>
              <li><strong>미분류</strong> — 분류 실패. 점검 필요</li>
            </ul>
          </details>
        </div>
      </div>
    </section>
  );
}

function SummaryCard({
  tone,
  title,
  count,
  total,
  note,
}: {
  tone: "emerald" | "amber" | "rose";
  title: string;
  count: number;
  total: number;
  note: string;
}) {
  const toneClass = {
    emerald:
      "bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-900",
    amber: "bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-900",
    rose: "bg-rose-50 dark:bg-rose-950/30 border-rose-200 dark:border-rose-900",
  }[tone];
  return (
    <div className={`rounded-xl border p-4 ${toneClass}`}>
      <div className="text-xs font-medium">{title}</div>
      <div className="text-2xl font-bold tabular-nums mt-1">{formatNum(count)}</div>
      <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">
        {pct(count, total)}% · {note}
      </div>
    </div>
  );
}

function GroupTable({
  rows,
  totalDocs,
  headerLabel,
}: {
  rows: DashboardGroupRow[];
  totalDocs: number;
  headerLabel: string;
}) {
  return (
    <table className="w-full text-sm">
      <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
        <tr>
          <th className="text-left px-4 py-3 font-medium">{headerLabel}</th>
          <th className="text-right px-4 py-3 font-medium">사이트</th>
          <th className="text-right px-4 py-3 font-medium">문서</th>
          <th className="text-right px-4 py-3 font-medium">PDF</th>
          <th className="text-right px-4 py-3 font-medium">TXT</th>
          <th className="text-left px-4 py-3 font-medium w-[28%]">비중</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
        {rows.map((r) => {
          const share = pct(r.docs, totalDocs);
          return (
            <tr key={r.label} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
              <td className="px-4 py-2.5 font-medium">{r.label}</td>
              <td className="px-4 py-2.5 text-right font-mono">{formatNum(r.sites)}</td>
              <td className="px-4 py-2.5 text-right font-mono">{formatNum(r.docs)}</td>
              <td className="px-4 py-2.5 text-right font-mono text-slate-500 dark:text-slate-400">
                {formatNum(r.pdf)}
              </td>
              <td className="px-4 py-2.5 text-right font-mono text-slate-500 dark:text-slate-400">
                {formatNum(r.txt)}
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <div className="flex-1 h-2 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${
                        share >= 30
                          ? "bg-indigo-500"
                          : share >= 10
                            ? "bg-blue-500"
                            : "bg-emerald-500"
                      }`}
                      style={{ width: `${share}%` }}
                    />
                  </div>
                  <span className="text-xs text-slate-500 dark:text-slate-400 w-10 text-right tabular-nums">
                    {share}%
                  </span>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function EmptyHint() {
  return (
    <div className="px-4 py-10 text-center text-slate-400 dark:text-slate-500 text-sm">
      집계할 데이터가 없습니다.
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
  subtitle,
  tone,
}: {
  label: string;
  value: string;
  subtitle?: string;
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
      {subtitle && (
        <div className="text-xs opacity-70 mt-1">{subtitle}</div>
      )}
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

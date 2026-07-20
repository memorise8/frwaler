import Link from "next/link";
import {
  getCollectionReport,
  getPdfGapDiagnosis,
  PDF_GAP_REASON_LABEL,
  type SiteGap,
} from "@/lib/db";

export const dynamic = "force-dynamic";

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

function pct(part: number, whole: number): string {
  if (!whole) return "—";
  return ((part / whole) * 100).toFixed(1) + "%";
}

function Bar({ value, max, color = "bg-indigo-500" }: { value: number; max: number; color?: string }) {
  const w = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="w-full h-2 bg-slate-200 dark:bg-slate-800 rounded overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${w}%` }} />
    </div>
  );
}

export default async function CollectionReportPage() {
  const r = getCollectionReport();
  const diag = getPdfGapDiagnosis();
  const dominantReasonCounts = new Map<string, number>();
  for (const v of Object.values(diag)) {
    dominantReasonCounts.set(v.dominant, (dominantReasonCounts.get(v.dominant) || 0) + 1);
  }
  const reasonsSorted = Array.from(dominantReasonCounts.entries()).sort(
    (a, b) => b[1] - a[1],
  );
  const collectedFromInput = pct(r.collectedSites, r.inputUniqueHosts || r.inputEntries);
  const pdfPctOfDocs = pct(r.totalPdfDownloaded, r.totalDocs);
  const textPctOfPdf = pct(r.totalTextExtracted, r.totalPdfDownloaded);
  const sumPctOfText = pct(r.totalSummary, r.totalTextExtracted);
  const pdfFailureRate = pct(r.totalPdfFailed, r.totalPdfDownloaded + r.totalPdfFailed);

  const topGaps: SiteGap[] = [...r.siteGaps]
    .filter((g) => g.pdf_failed > 0)
    .sort((a, b) => b.pdf_failed - a.pdf_failed)
    .slice(0, 20);

  return (
    <div className="px-6 py-8 max-w-7xl mx-auto space-y-8">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold">수집 현황 리포트</h1>
          <p className="text-sm text-slate-500 mt-1">
            입력 → 등록 → 수집 funnel + 사이트별 PDF 누락 사유 + 복구 카테고리
          </p>
        </div>
        <nav className="flex gap-2 text-sm">
          <Link href="/" className="px-3 py-1.5 rounded border border-slate-300 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-900">홈</Link>
          <Link href="/admin/status" className="px-3 py-1.5 rounded border border-slate-300 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-900">사이트 진행도</Link>
          <Link href="/admin/summary" className="px-3 py-1.5 rounded border border-slate-300 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-900">요약</Link>
        </nav>
      </div>

      {/* Funnel */}
      <section>
        <h2 className="text-lg font-semibold mb-3">📥 입력 → 등록 → 수집 Funnel</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
            <div className="text-xs text-slate-500">입력 URL entries</div>
            <div className="text-2xl font-mono tabular-nums mt-1">{formatNum(r.inputEntries)}</div>
            <div className="text-xs opacity-70 mt-1">unique hosts: {formatNum(r.inputUniqueHosts)}</div>
          </div>
          <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
            <div className="text-xs text-slate-500">등록 크롤러</div>
            <div className="text-2xl font-mono tabular-nums mt-1">{formatNum(r.registeredCrawlers)}</div>
            <div className="text-xs opacity-70 mt-1">custom + configs + built-in</div>
          </div>
          <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
            <div className="text-xs text-slate-500">수집 완료 사이트</div>
            <div className="text-2xl font-mono tabular-nums mt-1">{formatNum(r.collectedSites)}</div>
            <div className="text-xs opacity-70 mt-1">libertree.db에 1건 이상 적재</div>
          </div>
          <div className="rounded-lg border border-emerald-200 dark:border-emerald-800 p-4 bg-emerald-50/50 dark:bg-emerald-950/30">
            <div className="text-xs text-emerald-700 dark:text-emerald-300">입력 대비 수집률</div>
            <div className="text-2xl font-mono tabular-nums mt-1">{collectedFromInput}</div>
            <div className="text-xs opacity-70 mt-1">기준: unique hosts</div>
          </div>
        </div>
      </section>

      {/* Stage funnel */}
      <section>
        <h2 className="text-lg font-semibold mb-3">📊 문서 단계별 통과율</h2>
        <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-5 space-y-4">
          <div>
            <div className="flex items-center justify-between text-sm">
              <span>총 메타데이터 수집</span>
              <span className="font-mono">{formatNum(r.totalDocs)}</span>
            </div>
            <Bar value={r.totalDocs} max={r.totalDocs} color="bg-indigo-500" />
          </div>
          <div>
            <div className="flex items-center justify-between text-sm">
              <span>PDF 다운로드 ({pdfPctOfDocs} of docs)</span>
              <span className="font-mono">{formatNum(r.totalPdfDownloaded)}</span>
            </div>
            <Bar value={r.totalPdfDownloaded} max={r.totalDocs} color="bg-blue-500" />
          </div>
          <div>
            <div className="flex items-center justify-between text-sm">
              <span>텍스트 추출 ({textPctOfPdf} of PDF)</span>
              <span className="font-mono">{formatNum(r.totalTextExtracted)}</span>
            </div>
            <Bar value={r.totalTextExtracted} max={r.totalDocs} color="bg-cyan-500" />
          </div>
          <div>
            <div className="flex items-center justify-between text-sm">
              <span>요약 생성 ({sumPctOfText} of text)</span>
              <span className="font-mono">{formatNum(r.totalSummary)}</span>
            </div>
            <Bar value={r.totalSummary} max={r.totalDocs} color="bg-emerald-500" />
          </div>
        </div>
      </section>

      {/* PDF gap analysis */}
      <section>
        <h2 className="text-lg font-semibold mb-3">🩻 PDF 누락 사유 분석</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
          <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
            <div className="text-xs text-slate-500">PDF 다운로드 성공</div>
            <div className="text-xl font-mono tabular-nums mt-1">{formatNum(r.totalPdfDownloaded)}</div>
          </div>
          <div className="rounded-lg border border-amber-200 dark:border-amber-800 p-4 bg-amber-50/50 dark:bg-amber-950/30">
            <div className="text-xs text-amber-700 dark:text-amber-300">pdf_url 있지만 다운로드 실패</div>
            <div className="text-xl font-mono tabular-nums mt-1">{formatNum(r.totalPdfFailed)}</div>
            <div className="text-xs opacity-70 mt-1">실패율: {pdfFailureRate}</div>
          </div>
          <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
            <div className="text-xs text-slate-500">pdf_url 자체 없음 (HTML/메타만)</div>
            <div className="text-xl font-mono tabular-nums mt-1">{formatNum(r.totalNoPdfUrl)}</div>
            <div className="text-xs opacity-70 mt-1">정부/뉴스 사이트 정상</div>
          </div>
        </div>

        <details className="rounded-lg border border-slate-200 dark:border-slate-800">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium select-none">
            PDF 다운로드 실패 TOP 20 (사이트별)
          </summary>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-y border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50">
                  <th className="text-left px-4 py-2">순위</th>
                  <th className="text-left px-4 py-2">site_id</th>
                  <th className="text-right px-4 py-2">메타 총수</th>
                  <th className="text-right px-4 py-2">PDF 받음</th>
                  <th className="text-right px-4 py-2">실패</th>
                  <th className="text-right px-4 py-2">실패율</th>
                  <th className="text-left px-4 py-2">실패 사유 (sample 진단)</th>
                </tr>
              </thead>
              <tbody>
                {topGaps.map((g, i) => {
                  const d = diag[g.site_id];
                  const reason = d ? PDF_GAP_REASON_LABEL[d.dominant] || d.dominant : "(미진단)";
                  const detail = d
                    ? Object.entries(d.reasons)
                        .map(([k, v]) => `${k}×${v}`)
                        .join(", ")
                    : "";
                  return (
                    <tr key={g.site_id} className="border-b border-slate-100 dark:border-slate-900">
                      <td className="px-4 py-2">{i + 1}</td>
                      <td className="px-4 py-2 font-mono text-xs">{g.site_id}</td>
                      <td className="px-4 py-2 text-right font-mono tabular-nums">{formatNum(g.total)}</td>
                      <td className="px-4 py-2 text-right font-mono tabular-nums">{formatNum(g.pdf_downloaded)}</td>
                      <td className="px-4 py-2 text-right font-mono tabular-nums text-amber-600 dark:text-amber-400">
                        {formatNum(g.pdf_failed)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono tabular-nums">
                        {pct(g.pdf_failed, g.pdf_downloaded + g.pdf_failed)}
                      </td>
                      <td className="px-4 py-2 text-xs">
                        <div>{reason}</div>
                        {detail && (
                          <div className="text-slate-500 mt-0.5">{detail}</div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </details>
      </section>

      {/* PDF gap diagnosis summary */}
      {reasonsSorted.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold mb-3">🔬 누락 사유 자동 진단 (사이트 단위)</h2>
          <p className="text-sm text-slate-500 mb-3">
            누락 있는 사이트 {Object.keys(diag).length}개에 대해 sample 3개를 HEAD probe 한 결과.
            결과 출처:{" "}
            <code className="px-1 bg-slate-100 dark:bg-slate-900 rounded">
              data/audit/pdf_gap_diagnosis.json
            </code>
          </p>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {reasonsSorted.map(([reason, count]) => (
              <div
                key={reason}
                className="rounded-lg border border-slate-200 dark:border-slate-800 p-4"
              >
                <div className="text-xs text-slate-500">{PDF_GAP_REASON_LABEL[reason] || reason}</div>
                <div className="text-xl font-mono tabular-nums mt-1">{formatNum(count)}</div>
                <Bar
                  value={count}
                  max={Object.keys(diag).length}
                  color="bg-slate-500"
                />
                <div className="text-xs text-slate-500 mt-1">사이트 수</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Recovery categories */}
      <section>
        <h2 className="text-lg font-semibold mb-3">🛠 입력 1,994 entries 복구 분류</h2>
        <p className="text-sm text-slate-500 mb-3">
          출처: <code className="px-1 bg-slate-100 dark:bg-slate-900 rounded">data/audit/site_recovery_plan.csv</code>
        </p>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {r.recoveryCategories.map((c) => (
            <div key={c.category} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
              <div className="text-xs text-slate-500">{c.category}</div>
              <div className="text-xl font-mono tabular-nums mt-1">{formatNum(c.count)}</div>
              <Bar value={c.count} max={r.inputEntries} color="bg-slate-500" />
            </div>
          ))}
        </div>
        <div className="mt-4 text-sm text-slate-600 dark:text-slate-400 space-y-1.5">
          <p>
            <strong className="text-slate-900 dark:text-slate-100">⚫ 절대불가</strong>: dead 페이지/영구 IP 차단. archive.org 외에 회수 수단 없음.
          </p>
          <p>
            <strong className="text-slate-900 dark:text-slate-100">🚫 정책상포기</strong>: robots.txt 거부. 사이트 운영자에게 API 요청이 정도.
          </p>
          <p>
            <strong className="text-slate-900 dark:text-slate-100">🔐 사람개입필요-회원가입</strong>: auth_blocked. 회원가입 + 쿠키 추출 → playwright_fetcher.py에 주입.
          </p>
          <p>
            <strong className="text-slate-900 dark:text-slate-100">🔄 자동회복중</strong>: 시스템이 자동 batch에서 처리 중. 사용자 개입 불필요.
          </p>
          <p>
            <strong className="text-slate-900 dark:text-slate-100">✅ 수집완료</strong>: libertree.db에 적재됨. 일부는 cap 도달로 추가 분 대기.
          </p>
        </div>
      </section>
    </div>
  );
}

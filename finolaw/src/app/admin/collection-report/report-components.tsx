import Link from "next/link";
import type { RecoveryCategory, SiteGap } from "@/lib/db";

export interface ExceptionRow extends SiteGap {
  rank: number;
  diagnosis: string;
  detail: string | null;
}

export interface DiagnosisReason {
  label: string;
  count: number;
}

interface StatusBandProps {
  inputEntries: number;
  inputUniqueHosts: number;
  registeredCrawlers: number;
  collectedSites: number;
  collectedRate: string;
  totalDocs: number;
  totalPdfDownloaded: number;
  totalTextExtracted: number;
  totalSummary: number;
  pdfRate: string;
  textRate: string;
  summaryRate: string;
}

function formatNumber(value: number): string {
  return value.toLocaleString("ko-KR");
}

function EmptyState({ children }: { children: string }) {
  return (
    <p className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600 dark:border-slate-700 dark:bg-slate-900/60 dark:text-slate-300">
      {children}
    </p>
  );
}

function MetricRow({ label, value, max, description }: {
  label: string;
  value: number;
  max: number;
  description: string;
}) {
  const width = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(10rem,1fr)_minmax(12rem,2fr)_auto] sm:items-center">
      <div>
        <p className="text-sm font-medium text-slate-800 dark:text-slate-100">{label}</p>
        <p className="text-xs text-slate-500 dark:text-slate-400">{description}</p>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800" aria-hidden="true">
        <div className="h-full rounded-full bg-blue-700 dark:bg-blue-400" style={{ width: `${width}%` }} />
      </div>
      <p className="font-mono text-sm tabular-nums text-slate-900 dark:text-white">{formatNumber(value)}건</p>
    </div>
  );
}

export function ReportHeader() {
  const linkClass = "rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:border-blue-700 hover:text-blue-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 dark:border-slate-700 dark:text-slate-200 dark:hover:border-blue-300 dark:hover:text-blue-200 dark:focus-visible:outline-blue-300";
  return (
    <header className="border-b border-slate-200 pb-5 dark:border-slate-800">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-2xl">
          <p className="text-xs font-semibold tracking-[0.16em] text-blue-800 dark:text-blue-300">LIBERTREE · COLLECTION REPORT</p>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-slate-950 dark:text-white sm:text-3xl">수집 현황 리포트</h1>
          <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">현재 저장된 수집 집계를 바탕으로 문서 단계와 PDF 예외를 함께 확인합니다.</p>
        </div>
        <nav aria-label="리포트 이동" className="flex flex-wrap gap-2">
          <Link href="/" className={linkClass}>홈</Link>
          <Link href="/admin/status" className={linkClass}>사이트 진행도</Link>
          <Link href="/admin/summary" className={linkClass}>요약</Link>
        </nav>
      </div>
    </header>
  );
}

export function StatusBand(props: StatusBandProps) {
  const isEmpty = props.totalDocs === 0 && props.inputEntries === 0;
  return (
    <section aria-labelledby="report-status" className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="text-xs font-semibold tracking-[0.14em] text-slate-500 dark:text-slate-400">STATUS</p>
          <h2 id="report-status" className="mt-1 text-lg font-bold text-slate-950 dark:text-white">입력부터 요약까지의 현재 상태</h2>
        </div>
        <p className="rounded-full bg-blue-50 px-3 py-1 text-xs font-semibold text-blue-800 dark:bg-blue-950/50 dark:text-blue-200">읽기 전용 집계</p>
      </div>
      {isEmpty ? <EmptyState>표시할 수집 집계가 없습니다. 데이터가 준비되면 이 리포트에 현재 상태가 나타납니다.</EmptyState> : (
        <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-slate-200 bg-slate-200 dark:border-slate-800 dark:bg-slate-800">
            <Stat label="입력 URL" value={props.inputEntries} description={`고유 호스트 ${formatNumber(props.inputUniqueHosts)}곳`} />
            <Stat label="등록 수집기" value={props.registeredCrawlers} description="현재 등록 기준" />
            <Stat label="수집 사이트" value={props.collectedSites} description={`입력 대비 ${props.collectedRate}`} />
            <Stat label="메타데이터" value={props.totalDocs} description="저장 문서 기준" />
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <h3 className="text-sm font-bold text-slate-900 dark:text-white">문서 단계별 통과 현황</h3>
            <div className="mt-4 space-y-4">
              <MetricRow label="PDF 다운로드" value={props.totalPdfDownloaded} max={props.totalDocs} description={`메타데이터 대비 ${props.pdfRate}`} />
              <MetricRow label="텍스트 추출" value={props.totalTextExtracted} max={props.totalDocs} description={`PDF 대비 ${props.textRate}`} />
              <MetricRow label="요약 생성" value={props.totalSummary} max={props.totalDocs} description={`텍스트 대비 ${props.summaryRate}`} />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, description }: { label: string; value: number; description: string }) {
  return <div className="bg-white p-4 dark:bg-slate-950"><p className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</p><p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-slate-950 dark:text-white">{formatNumber(value)}</p><p className="mt-1 text-xs text-slate-600 dark:text-slate-300">{description}</p></div>;
}

interface ExceptionTableProps { rows: readonly ExceptionRow[]; downloaded: number; failed: number; noPdfUrl: number; failureRate: string; }

export function ExceptionTable({ rows, downloaded, failed, noPdfUrl, failureRate }: ExceptionTableProps) {
  return (
    <section aria-labelledby="pdf-exceptions" className="space-y-4">
      <div><p className="text-xs font-semibold tracking-[0.14em] text-slate-500 dark:text-slate-400">EXCEPTIONS</p><h2 id="pdf-exceptions" className="mt-1 text-lg font-bold text-slate-950 dark:text-white">PDF 누락과 사이트별 예외</h2></div>
      <div className="grid gap-3 sm:grid-cols-3"><Stat label="PDF 다운로드 성공" value={downloaded} description="현재 저장 문서 기준" /><Stat label="다운로드 실패" value={failed} description={`실패율 ${failureRate}`} /><Stat label="PDF 주소 없음" value={noPdfUrl} description="HTML 또는 메타데이터만 보유" /></div>
      {rows.length === 0 ? <EmptyState>PDF 다운로드 실패로 분류된 사이트가 없습니다. 진단 대상이 생기면 이 표에 우선순위가 표시됩니다.</EmptyState> : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 dark:border-slate-800 dark:focus-visible:outline-blue-300" tabIndex={0} aria-label="PDF 다운로드 실패 사이트 표. 작은 화면에서는 가로로 스크롤할 수 있습니다.">
          <table className="min-w-[760px] w-full border-collapse text-left text-sm"><caption className="caption-top px-4 py-3 text-left text-sm text-slate-600 dark:text-slate-300">다운로드 실패 건수 기준 상위 {rows.length}개 사이트</caption><thead className="border-y border-slate-200 bg-slate-50 text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300"><tr><th scope="col" className="px-4 py-3">순위</th><th scope="col" className="px-4 py-3">사이트</th><th scope="col" className="px-4 py-3 text-right">메타</th><th scope="col" className="px-4 py-3 text-right">PDF</th><th scope="col" className="px-4 py-3 text-right">실패</th><th scope="col" className="px-4 py-3 text-right">실패율</th><th scope="col" className="px-4 py-3">진단</th></tr></thead><tbody>{rows.map((row) => <tr key={row.site_id} className="border-b border-slate-100 last:border-0 dark:border-slate-900"><td className="px-4 py-3 font-mono tabular-nums">{row.rank}</td><th scope="row" className="px-4 py-3 font-mono text-xs font-medium">{row.site_id}</th><td className="px-4 py-3 text-right font-mono tabular-nums">{formatNumber(row.total)}</td><td className="px-4 py-3 text-right font-mono tabular-nums">{formatNumber(row.pdf_downloaded)}</td><td className="px-4 py-3 text-right font-mono tabular-nums">{formatNumber(row.pdf_failed)}</td><td className="px-4 py-3 text-right font-mono tabular-nums">{row.pdf_downloaded + row.pdf_failed === 0 ? "집계 없음" : `${((row.pdf_failed / (row.pdf_downloaded + row.pdf_failed)) * 100).toFixed(1)}%`}</td><td className="px-4 py-3"><p>{row.diagnosis}</p>{row.detail ? <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{row.detail}</p> : null}</td></tr>)}</tbody></table>
        </div>
      )}
    </section>
  );
}

interface RecoverySectionProps { diagnosisReasons: readonly DiagnosisReason[]; diagnosedSites: number; categories: readonly RecoveryCategory[]; inputEntries: number; }

export function RecoverySection({ diagnosisReasons, diagnosedSites, categories, inputEntries }: RecoverySectionProps) {
  return <section className="grid gap-6 lg:grid-cols-2"><div><p className="text-xs font-semibold tracking-[0.14em] text-slate-500 dark:text-slate-400">DIAGNOSIS</p><h2 className="mt-1 text-lg font-bold text-slate-950 dark:text-white">누락 사유 진단</h2>{diagnosisReasons.length === 0 ? <div className="mt-4"><EmptyState>진단 정보가 아직 없습니다. 이는 진단 대상이 없거나 진단 결과를 사용할 수 없는 상태일 수 있습니다.</EmptyState></div> : <ul className="mt-4 divide-y divide-slate-200 rounded-xl border border-slate-200 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-950">{diagnosisReasons.map((reason) => <li key={reason.label} className="flex items-center justify-between gap-4 px-4 py-3"><span className="text-sm text-slate-700 dark:text-slate-200">{reason.label}</span><span className="font-mono text-sm tabular-nums">{formatNumber(reason.count)}곳</span></li>)}</ul>}<p className="mt-3 text-xs text-slate-500 dark:text-slate-400">진단에 포함된 사이트: {formatNumber(diagnosedSites)}곳</p></div><div><p className="text-xs font-semibold tracking-[0.14em] text-slate-500 dark:text-slate-400">RECOVERY</p><h2 className="mt-1 text-lg font-bold text-slate-950 dark:text-white">입력 항목 분류</h2>{categories.length === 0 ? <div className="mt-4"><EmptyState>표시할 분류 집계가 없습니다. 입력 항목이 준비되면 분류별 수가 나타납니다.</EmptyState></div> : <ul className="mt-4 divide-y divide-slate-200 rounded-xl border border-slate-200 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-950">{categories.map((category) => <li key={category.category} className="flex items-center justify-between gap-4 px-4 py-3"><span className="text-sm text-slate-700 dark:text-slate-200">{category.category}</span><span className="font-mono text-sm tabular-nums">{formatNumber(category.count)}건</span></li>)}</ul>}<p className="mt-3 text-xs text-slate-500 dark:text-slate-400">현재 입력 집계 기준: {formatNumber(inputEntries)}건</p></div></section>;
}

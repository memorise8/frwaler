import {
  getCollectionReport,
  getPdfGapDiagnosis,
  PDF_GAP_REASON_LABEL,
  type SiteGap,
} from "@/lib/db";
import {
  ExceptionTable,
  RecoverySection,
  ReportHeader,
  StatusBand,
  type DiagnosisReason,
  type ExceptionRow,
} from "./report-components";

export const dynamic = "force-dynamic";

function percent(part: number, whole: number): string {
  if (!whole) return "집계 없음";
  return `${((part / whole) * 100).toFixed(1)}%`;
}

export default async function CollectionReportPage() {
  const r = getCollectionReport();
  const diag = getPdfGapDiagnosis();
  const diagnosisCounts = new Map<string, number>();

  for (const value of Object.values(diag)) {
    diagnosisCounts.set(value.dominant, (diagnosisCounts.get(value.dominant) ?? 0) + 1);
  }

  const exceptionRows: ExceptionRow[] = [...r.siteGaps]
    .filter((gap) => gap.pdf_failed > 0)
    .sort((left, right) => right.pdf_failed - left.pdf_failed)
    .slice(0, 20)
    .map((gap: SiteGap, index) => {
      const diagnosis = diag[gap.site_id];
      const detail = diagnosis
        ? Object.entries(diagnosis.reasons)
            .map(([reason, count]) => `${reason} ${count}건`)
            .join(", ")
        : null;

      return {
        ...gap,
        rank: index + 1,
        diagnosis: diagnosis
          ? PDF_GAP_REASON_LABEL[diagnosis.dominant] ?? diagnosis.dominant
          : "진단 정보 없음",
        detail,
      };
    });

  const diagnosisReasons: DiagnosisReason[] = Array.from(diagnosisCounts.entries())
    .sort((left, right) => right[1] - left[1])
    .map(([reason, count]) => ({
      label: PDF_GAP_REASON_LABEL[reason] ?? reason,
      count,
    }));

  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 sm:py-8">
      <ReportHeader />
      <StatusBand
        inputEntries={r.inputEntries}
        inputUniqueHosts={r.inputUniqueHosts}
        registeredCrawlers={r.registeredCrawlers}
        collectedSites={r.collectedSites}
        collectedRate={percent(r.collectedSites, r.inputUniqueHosts || r.inputEntries)}
        totalDocs={r.totalDocs}
        totalPdfDownloaded={r.totalPdfDownloaded}
        totalTextExtracted={r.totalTextExtracted}
        totalSummary={r.totalSummary}
        pdfRate={percent(r.totalPdfDownloaded, r.totalDocs)}
        textRate={percent(r.totalTextExtracted, r.totalPdfDownloaded)}
        summaryRate={percent(r.totalSummary, r.totalTextExtracted)}
      />
      <ExceptionTable
        rows={exceptionRows}
        downloaded={r.totalPdfDownloaded}
        failed={r.totalPdfFailed}
        noPdfUrl={r.totalNoPdfUrl}
        failureRate={percent(r.totalPdfFailed, r.totalPdfDownloaded + r.totalPdfFailed)}
      />
      <RecoverySection
        diagnosisReasons={diagnosisReasons}
        diagnosedSites={Object.keys(diag).length}
        categories={r.recoveryCategories}
        inputEntries={r.inputEntries}
      />
    </main>
  );
}

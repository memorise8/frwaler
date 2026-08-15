import Link from "next/link";
import { notFound } from "next/navigation";
import { AUDIT_DATE, getCrawlerHealth } from "@/lib/crawler-health";
import RunPanel from "./run-panel";

export const dynamic = "force-dynamic";

export default async function CrawlerDetail({ params }: Readonly<{ params: Promise<{ readonly siteId: string }> }>) {
  const { siteId } = await params;
  const crawler = getCrawlerHealth().find((item) => item.siteId === siteId);
  if (!crawler) notFound();
  return (
    <div className="detail-page">
      <Link className="back-link" href="/">← 크롤러 목록</Link>
      <header className="detail-header">
        <div><p className="eyebrow">CRAWLER PROFILE</p><h1>{crawler.siteName || crawler.siteId}</h1><code>{crawler.siteId}</code></div>
        <span className={`health-badge health-badge--${crawler.status}`}><i />{crawler.status === "healthy" ? `이전 검증 성공 (${AUDIT_DATE})` : `이전 검증 실패 (${AUDIT_DATE})`}</span>
      </header>
      <aside className="snapshot-note">
        <strong>스냅샷 안내</strong>
        <span>
          상태 배지·과거 수집 건수·실패 사유는 모두 {AUDIT_DATE}에 기록된 스냅샷이며 실시간 상태가 아닙니다.
          이후 실행한 수집은 반영되지 않습니다. 현재 상태는 아래에서 직접 실행해 확인하세요.
        </span>
      </aside>
      <dl className="detail-facts">
        <div><dt>국가</dt><dd>{crawler.country}</dd></div><div><dt>대륙</dt><dd>{crawler.continent}</dd></div><div><dt>자료 유형</dt><dd>{crawler.docType}</dd></div><div><dt>과거 수집</dt><dd>{crawler.collected.toLocaleString("ko-KR")}건</dd></div><div><dt>마지막 검증</dt><dd>{AUDIT_DATE}</dd></div>
      </dl>
      {crawler.reason && <aside className="failure-reason"><strong>{crawler.category}</strong><span>{crawler.reason}</span></aside>}
      <RunPanel siteId={crawler.siteId} status={crawler.status} category={crawler.category} reason={crawler.reason} />
    </div>
  );
}

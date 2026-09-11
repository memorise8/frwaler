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
        <span className={`health-badge health-badge--${crawler.status}`}><i />{crawler.status === "healthy" ? "이전 검증 성공" : "이전 검증 실패"}</span>
      </header>
      <dl className="detail-facts">
        <div><dt>국가</dt><dd>{crawler.country}</dd></div><div><dt>대륙</dt><dd>{crawler.continent}</dd></div><div><dt>자료 유형</dt><dd>{crawler.docType}</dd></div><div><dt>과거 수집</dt><dd>{crawler.collected.toLocaleString("ko-KR")}건</dd></div><div><dt>마지막 검증</dt><dd>{AUDIT_DATE}</dd></div>
      </dl>
      {crawler.reason && <aside className="failure-reason"><strong>{crawler.category}</strong><span>{crawler.reason}</span></aside>}
      <RunPanel siteId={crawler.siteId} />
    </div>
  );
}

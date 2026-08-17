import Link from "next/link";
import { notFound } from "next/navigation";
import { AUDIT_DATE, getCrawlerHealth } from "@/lib/crawler-health";
import { crawlerDocumentsHref, formatEvidenceDate, summarizeCrawlerEvidence } from "@/lib/crawler-evidence";
import { getDocumentCatalogue } from "@/lib/document-catalogue";
import { getVerificationStats } from "@/lib/database-stats";
import { contradictsAudit, describeVerification, indexVerification, resolveVerificationState, VERIFICATION_LABEL } from "@/lib/crawler-verification";
import RunPanel from "@/components/run-panel";

export const dynamic = "force-dynamic";

const EVIDENCE_PAGE_SIZE = 5;

export default async function CrawlerDetail({ params }: Readonly<{ params: Promise<{ readonly siteId: string }> }>) {
  const { siteId } = await params;
  const crawler = getCrawlerHealth().find((item) => item.siteId === siteId);
  if (!crawler) notFound();
  const catalogueResult = await getDocumentCatalogue(new URLSearchParams({
    site_id: siteId, sort: "collected_desc", page_size: String(EVIDENCE_PAGE_SIZE),
  }));
  const evidence = summarizeCrawlerEvidence(catalogueResult);
  const verificationRow = indexVerification((await getVerificationStats())?.sites).get(siteId);
  const verified = resolveVerificationState(verificationRow);
  const documentsHref = crawlerDocumentsHref(siteId);
  return (
    <div className="detail-page">
      <Link className="back-link" href="/crawlers">← 크롤러 상태</Link>
      <header className="detail-header">
        <div><p className="eyebrow">CRAWLER PROFILE</p><h1>{crawler.siteName || crawler.siteId}</h1><code>{crawler.siteId}</code></div>
        <span className={`health-badge health-badge--${crawler.status}`}><i />{crawler.status === "healthy" ? `이전 검증 성공 (${AUDIT_DATE})` : `이전 검증 실패 (${AUDIT_DATE})`}</span>
      </header>
      <aside className="snapshot-note">
        <strong>스냅샷 안내</strong>
        <span>
          상태 배지·과거 수집 건수·실패 사유는 모두 {AUDIT_DATE}에 <strong>다른 네트워크에서</strong> 기록된 스냅샷이며
          갱신되지 않습니다. 이 설치에서 실행한 결과는 아래 &ldquo;여기 결과&rdquo;에 있습니다.
        </span>
      </aside>

      <aside className={`snapshot-note${contradictsAudit(crawler.status, verified) ? "" : " snapshot-note--plain"}`}>
        <strong>여기 결과 · <span className={`verify-tag verify-tag--${verified}`}>{VERIFICATION_LABEL[verified]}</span></strong>
        <span>{describeVerification(crawler.status, verified, verificationRow)}</span>
      </aside>
      <dl className="detail-facts">
        <div><dt>국가</dt><dd>{crawler.country}</dd></div><div><dt>대륙</dt><dd>{crawler.continent}</dd></div><div><dt>자료 유형</dt><dd>{crawler.docType}</dd></div><div><dt>과거 수집</dt><dd>{crawler.collected.toLocaleString("ko-KR")}건</dd></div><div><dt>마지막 검증</dt><dd>{AUDIT_DATE}</dd></div>
      </dl>
      {crawler.reason && <aside className="failure-reason"><strong>{crawler.category}</strong><span>{crawler.reason}</span></aside>}

      <section className="catalogue-section" aria-labelledby="crawler-evidence-heading">
        <div className="section-heading">
          <div><p className="eyebrow">DATABASE EVIDENCE</p><h2 id="crawler-evidence-heading">실제 수집 문서</h2></div>
          <p>{evidence.kind === "unavailable" ? "실시간 조회 불가" : `실시간 조회 ${new Date(evidence.measuredAt).toLocaleString("ko-KR")}`}</p>
        </div>

        {evidence.kind === "unavailable" && (
          <aside className="snapshot-note snapshot-note--unavailable">
            <strong>실데이터 연결 대기</strong>
            <span>
              BE 또는 PostgreSQL에 연결할 수 없어 이 수집기가 실제로 수집한 문서를 지금 확인할 수 없습니다.
              위 과거 수집 {crawler.collected.toLocaleString("ko-KR")}건은 {AUDIT_DATE}에 기록된 스냅샷 값이며, 실시간 상태와는 무관합니다.
            </span>
          </aside>
        )}

        {evidence.kind === "empty" && (
          <aside className="snapshot-note">
            <strong>수집 문서 없음</strong>
            <span>
              데이터베이스에는 이 사이트의 문서가 없습니다 (방금 조회). 위 과거 수집 {crawler.collected.toLocaleString("ko-KR")}건은 {AUDIT_DATE}에 기록된 스냅샷 값으로,
              지금 조회한 값과는 별개의 측정입니다. 두 값이 다르면 스냅샷 이후 상황이 바뀐 것입니다.
            </span>
          </aside>
        )}

        {evidence.kind === "found" && (
          <>
            <aside className="snapshot-note">
              <strong>스냅샷과 실시간 비교</strong>
              <span>
                위 과거 수집 {crawler.collected.toLocaleString("ko-KR")}건은 {AUDIT_DATE}에 기록된 스냅샷 값이고,
                아래 {evidence.total.toLocaleString("ko-KR")}건은 지금 데이터베이스를 조회한 값입니다. 두 수치는 측정 시점이 달라 다를 수 있습니다.
              </span>
            </aside>

            <div className="taxonomy-grid" aria-label="이 사이트의 실시간 문서 분포">
              <article className="taxonomy-panel">
                <div className="taxonomy-heading"><div><p className="eyebrow">DOC TYPES</p><h3>자료 유형</h3></div><span>실시간</span></div>
                <div className="type-grid">
                  {evidence.docTypes.map((item) => (
                    <Link href={crawlerDocumentsHref(siteId, { key: "doc_type", value: item.value })} key={item.value}>
                      <span>{item.value}</span><strong>{item.count.toLocaleString("ko-KR")}</strong>
                    </Link>
                  ))}
                </div>
              </article>
              <article className="taxonomy-panel">
                <div className="taxonomy-heading"><div><p className="eyebrow">LANGUAGES</p><h3>언어</h3></div><span>실시간</span></div>
                <div className="type-grid">
                  {evidence.languages.map((item) => (
                    <Link href={crawlerDocumentsHref(siteId, { key: "lang", value: item.value })} key={item.value}>
                      <span>{item.value === "unknown" ? "언어 미측정" : item.value.toUpperCase()}</span><strong>{item.count.toLocaleString("ko-KR")}</strong>
                    </Link>
                  ))}
                </div>
              </article>
            </div>

            <div className="document-results">
              <div className="section-heading">
                <div><p className="eyebrow">RECENTLY COLLECTED</p><h3>최근 수집 문서</h3></div>
                <p>{evidence.items.length.toLocaleString("ko-KR")}건 표시 · 전체 {evidence.total.toLocaleString("ko-KR")}건</p>
              </div>
              <ol className="document-list">
                {evidence.items.map((item) => (
                  <li key={item.seq_id}>
                    <article className="document-card">
                      <div className="document-card-index">{String(item.seq_id).padStart(6, "0")}</div>
                      <div className="document-card-main">
                        <div className="document-kicker"><span>{item.doc_type}</span><span>{item.lang === "unknown" ? "언어 미측정" : item.lang.toUpperCase()}</span></div>
                        <h3><Link href={`/documents/${item.seq_id}`}>{item.title}</Link></h3>
                        <div className="document-badges">
                          {item.has_pdf && <span className="available">PDF</span>}
                          {item.has_text && <span className="available">TEXT</span>}
                          {item.has_translation && <span className="translated">한국어 번역</span>}
                        </div>
                      </div>
                      <div className="document-card-side">
                        <span>발행 {formatEvidenceDate(item.published_date)}</span>
                        <span>수집 {formatEvidenceDate(item.collected_at)}</span>
                      </div>
                    </article>
                  </li>
                ))}
              </ol>
            </div>
          </>
        )}

        <Link className="run-link" href={documentsHref}>전체 문서 목록 보기 →</Link>
      </section>

      <RunPanel siteId={crawler.siteId} status={crawler.status} category={crawler.category} reason={crawler.reason} />
    </div>
  );
}

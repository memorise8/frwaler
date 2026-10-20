import Link from "next/link";
import { getCrawlerHealth } from "@/lib/crawler-health";
import { getDatabaseStats, getFreshnessStats } from "@/lib/database-stats";
import { findUncataloguedSites } from "@/lib/uncatalogued-sites";

export const dynamic = "force-dynamic";

export default async function CollectedState() {
  const rows = getCrawlerHealth();
  const databaseStats = await getDatabaseStats();
  const freshnessStats = await getFreshnessStats();
  const crawlerById = new Map(rows.map((row) => [row.siteId, row]));
  const uncataloguedSites = databaseStats ? findUncataloguedSites(databaseStats.by_site, rows.map((row) => row.siteId)) : [];
  const uncataloguedDocuments = uncataloguedSites.reduce((sum, site) => sum + site.documents, 0);
  const measuredCountryCounts = [...(databaseStats?.by_site ?? []).reduce((counts, item) => {
    const name = crawlerById.get(item.key)?.country ?? "기타";
    return counts.set(name, (counts.get(name) ?? 0) + item.documents);
  }, new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const measuredTypeCounts = [...(databaseStats?.by_site ?? []).reduce((counts, item) => {
    const name = crawlerById.get(item.key)?.docType ?? "기타";
    return counts.set(name, (counts.get(name) ?? 0) + item.documents);
  }, new Map<string, number>())].sort((a, b) => b[1] - a[1]);

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">COLLECTED DATA</p>
          <h1>지금까지 모은 것을,<br />실제 데이터로 확인합니다.</h1>
          <p className="lede">아래 수치는 스냅샷이 아니라 지금 데이터베이스를 조회한 값입니다. 수집기 목록과 달리 실행 결과가 곧바로 반영됩니다.</p>
        </div>
        <div className="audit-date"><span>MEASURED</span><strong>{databaseStats ? new Date(databaseStats.measured_at).toLocaleDateString("ko-KR") : "측정 불가"}</strong><small>{databaseStats ? new Date(databaseStats.measured_at).toLocaleTimeString("ko-KR") : "BE 연결 대기"}</small></div>
      </header>

      {databaseStats ? <>
        <div className="summary-grid">
          <Link className="summary-card" href="/documents"><span>전체 문서</span><strong>{databaseStats.overview.documents.toLocaleString("ko-KR")}</strong><small>{databaseStats.overview.sites.toLocaleString("ko-KR")}개 데이터 소스</small></Link>
          <Link className="summary-card summary-card--secondary" href="/documents?has_pdf=true"><span>PDF 확보</span><strong>{databaseStats.overview.pdf_downloaded.toLocaleString("ko-KR")}</strong><small>{(databaseStats.overview.pdf_bytes / 1024 ** 4).toFixed(1)} TB 메타데이터 합계</small></Link>
          <Link className="summary-card summary-card--secondary" href="/documents?has_text=true"><span>텍스트 확보</span><strong>{databaseStats.overview.text_extracted.toLocaleString("ko-KR")}</strong><small>추출 완료 문서</small></Link>
          <article className="summary-card summary-card--secondary"><span>최신 수집</span><strong className="summary-date">{databaseStats.overview.latest_collected_at ? new Date(databaseStats.overview.latest_collected_at).toLocaleDateString("ko-KR") : "없음"}</strong><small>DB collected_at 기준</small></article>
        </div>
        <aside className="snapshot-note"><strong>정합성</strong><span>고아 문서 {databaseStats.integrity.orphan_documents.toLocaleString("ko-KR")}건 · PDF 메타데이터 누락 {databaseStats.integrity.missing_pdf_metadata.toLocaleString("ko-KR")}건 · blob 파일 전수 검사는 별도 manifest 기준</span></aside>
        {uncataloguedSites.length > 0 && <aside className="snapshot-note"><strong>수집기 미등록</strong><span>{uncataloguedSites.length.toLocaleString("ko-KR")}개 데이터 소스 · 문서 {uncataloguedDocuments.toLocaleString("ko-KR")}건은 수집기 카탈로그에 없어 <Link href="/crawlers">크롤러 상태</Link> 화면에 나타나지 않습니다.</span></aside>}
        <div className="taxonomy-grid" aria-label="실제 문서 분포">
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><p className="eyebrow">DOCUMENTS BY COUNTRY</p><h2>국가별 문서</h2></div><span>DB 실측</span></div>
            <div className="taxonomy-bars">{measuredCountryCounts.slice(0, 10).map(([name, count]) => <div className="measure-row" key={name}><span>{name}</span><i><b style={{ width: `${(count / measuredCountryCounts[0]![1]) * 100}%` }} /></i><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
          </article>
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><p className="eyebrow">DOCUMENTS BY MATERIAL</p><h2>자료 유형별 문서</h2></div><span>미분류는 기타</span></div>
            <div className="type-grid">{measuredTypeCounts.map(([name, count]) => <div className="measure-tile" key={name}><span>{name}</span><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
          </article>
        </div>
      </> : <aside className="snapshot-note snapshot-note--unavailable"><strong>실데이터 연결 대기</strong><span>BE 또는 PostgreSQL에 연결할 수 없습니다. <Link href="/crawlers">크롤러 상태</Link> 화면의 감사 스냅샷은 계속 확인할 수 있습니다.</span></aside>}

      <section className="database-status">
        <div className="section-heading"><div><p className="eyebrow">FRESHNESS</p><h2>사이트 최신화</h2></div><p>{freshnessStats ? `측정 ${new Date(freshnessStats.measured_at).toLocaleString("ko-KR")}` : "현재 측정 불가"}</p></div>
        {freshnessStats ? <>
          <div className="summary-grid">
            <Link className="summary-card summary-card--secondary summary-card--good" href="/crawlers?freshness=within_7_days"><span>7일 이내</span><strong>{(freshnessStats.summary.distribution.within_7_days ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary" href="/crawlers?freshness=8_to_30_days"><span>8~30일</span><strong>{(freshnessStats.summary.distribution["8_to_30_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary" href="/crawlers?freshness=31_to_90_days"><span>31~90일</span><strong>{(freshnessStats.summary.distribution["31_to_90_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary summary-card--bad" href="/crawlers?freshness=over_90_or_never"><span>90일 초과·미수집</span><strong>{((freshnessStats.summary.distribution.over_90_days ?? 0) + freshnessStats.summary.never_collected).toLocaleString("ko-KR")}</strong><small>점검 대상</small></Link>
          </div>
          <p className="run-note">각 카드를 누르면 해당 사이트만 크롤러 상태 목록에 표시됩니다. 오래된 사이트를 골라 <Link href="/">수집</Link>에서 바로 실행하세요.</p>
        </> : <aside className="snapshot-note snapshot-note--unavailable"><strong>최신화 측정 대기</strong><span>BE 또는 PostgreSQL 연결 후 사이트별 마지막 수집일을 계산합니다.</span></aside>}
      </section>
    </div>
  );
}

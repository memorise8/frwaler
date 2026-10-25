import Link from "next/link";
import { getCrawlerHealth } from "@/lib/crawler-health";
import { getDatabaseStats, getFreshnessStats } from "@/lib/database-stats";
import { findUncataloguedSites } from "@/lib/uncatalogued-sites";
import { groupDocuments, type OtherPart } from "@/lib/collected-breakdown";
import { formatSeoul } from "@/lib/seoul-time";

export const dynamic = "force-dynamic";

export default async function CollectedState() {
  const rows = getCrawlerHealth();
  const databaseStats = await getDatabaseStats();
  const freshnessStats = await getFreshnessStats();
  const crawlerById = new Map(rows.map((row) => [row.siteId, row]));
  const uncataloguedSites = databaseStats ? findUncataloguedSites(databaseStats.by_site, rows.map((row) => row.siteId)) : [];
  const uncataloguedDocuments = uncataloguedSites.reduce((sum, site) => sum + site.documents, 0);
  const byCountry = groupDocuments(databaseStats?.by_site ?? [], crawlerById, "country");
  const byType = groupDocuments(databaseStats?.by_site ?? [], crawlerById, "docType");
  const measuredCountryCounts = byCountry.rows;
  const measuredTypeCounts = byType.rows;

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <h1>수집 상태</h1>
          <p className="lede">지금 저장소에 들어 있는 문서를 바로 센 값입니다. 수집이 끝나면 곧바로 반영됩니다.</p>
        </div>
        <div className="audit-date"><span>센 시각 (한국 시각)</span><strong>{databaseStats ? formatSeoul(databaseStats.measured_at) : "측정 불가"}</strong><small>{databaseStats ? "새로고침하면 다시 셉니다" : "서버 연결 대기"}</small></div>
      </header>

      {databaseStats ? <>
        <div className="summary-grid">
          <Link className="summary-card" href="/documents"><span>전체 문서</span><strong>{databaseStats.overview.documents.toLocaleString("ko-KR")}</strong><small>{databaseStats.overview.sites.toLocaleString("ko-KR")}개 사이트</small></Link>
          <Link className="summary-card summary-card--secondary" href="/documents?has_pdf=true"><span>PDF 확보</span><strong>{databaseStats.overview.pdf_downloaded.toLocaleString("ko-KR")}</strong><small>{(databaseStats.overview.pdf_bytes / 1024 ** 4).toFixed(1)} TB (기록된 파일 크기 합계)</small></Link>
          <Link className="summary-card summary-card--secondary" href="/documents?has_text=true"><span>텍스트 확보</span><strong>{databaseStats.overview.text_extracted.toLocaleString("ko-KR")}</strong><small>본문 글자를 뽑아 둔 문서</small></Link>
          <article className="summary-card summary-card--secondary"><span>마지막 수집</span><strong className="summary-date">{formatSeoul(databaseStats.overview.latest_collected_at, { fallback: "없음" })}</strong><small>가장 최근 수집 시각</small></article>
        </div>
        <aside className="snapshot-note"><strong>기록 점검</strong><span>사이트 없는 문서 {databaseStats.integrity.orphan_documents.toLocaleString("ko-KR")}건 · PDF가 있다고 되어 있지만 파일 정보가 빠진 문서 {databaseStats.integrity.missing_pdf_metadata.toLocaleString("ko-KR")}건 · 실제 파일 전체 확인은 따로 남기는 반영 기록으로 합니다.</span></aside>
        <details className="tech-info"><summary>기술 정보</summary><p className="run-note">사이트 없는 문서 = orphan_documents · 파일 정보 누락 = missing_pdf_metadata · 반영 기록 = blob manifest ({databaseStats.integrity.blob_check.status}) · 마지막 수집 = max(collected_at) · 센 시각 = measured_at {databaseStats.measured_at}</p></details>
        {uncataloguedSites.length > 0 && <aside className="snapshot-note"><strong>수집기 미등록</strong><span>{uncataloguedSites.length.toLocaleString("ko-KR")}개 사이트 · 문서 {uncataloguedDocuments.toLocaleString("ko-KR")}건은 수집기 목록에 없어 <Link href="/crawlers">크롤러 상태</Link> 화면에 나타나지 않습니다.</span></aside>}
        <div className="taxonomy-grid" aria-label="실제 문서 분포">
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><h2>국가별 문서</h2></div><span>지금 센 값</span></div>
            <div className="taxonomy-bars">{measuredCountryCounts.slice(0, 10).map(([name, count]) => <div className="measure-row" key={name}><span>{name === "기타" ? "기타 (국가 미정)" : name}</span><i><b style={{ width: `${(count / measuredCountryCounts[0]![1]) * 100}%` }} /></i><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
            <OtherBreakdown parts={byCountry.otherParts} />
          </article>
          <article className="taxonomy-panel">
            <div className="taxonomy-heading"><div><h2>자료 유형별 문서</h2></div><span>유형이 정해지지 않으면 기타</span></div>
            <div className="type-grid">{measuredTypeCounts.map(([name, count]) => <div className="measure-tile" key={name}><span>{name === "기타" ? "기타 (유형 미정)" : name}</span><strong>{count.toLocaleString("ko-KR")}</strong></div>)}</div>
            <OtherBreakdown parts={byType.otherParts} />
          </article>
        </div>
      </> : <aside className="snapshot-note snapshot-note--unavailable"><strong>저장소 연결 대기</strong><span>문서 저장소에 연결할 수 없습니다. <Link href="/crawlers">크롤러 상태</Link> 화면의 지난 점검 결과는 계속 볼 수 있습니다.</span></aside>}

      <section className="database-status">
        <div className="section-heading"><div><h2>사이트별 마지막 수집</h2></div><p>{freshnessStats ? `센 시각 ${formatSeoul(freshnessStats.measured_at)}` : "현재 측정 불가"}</p></div>
        {freshnessStats ? <>
          <div className="summary-grid">
            <Link className="summary-card summary-card--secondary summary-card--good" href="/crawlers?freshness=within_7_days"><span>7일 이내</span><strong>{(freshnessStats.summary.distribution.within_7_days ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary" href="/crawlers?freshness=8_to_30_days"><span>8~30일</span><strong>{(freshnessStats.summary.distribution["8_to_30_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary" href="/crawlers?freshness=31_to_90_days"><span>31~90일</span><strong>{(freshnessStats.summary.distribution["31_to_90_days"] ?? 0).toLocaleString("ko-KR")}</strong><small>사이트</small></Link>
            <Link className="summary-card summary-card--secondary summary-card--bad" href="/crawlers?freshness=over_90_or_never"><span>90일 초과·미수집</span><strong>{((freshnessStats.summary.distribution.over_90_days ?? 0) + freshnessStats.summary.never_collected).toLocaleString("ko-KR")}</strong><small>점검 대상</small></Link>
          </div>
          <p className="run-note">각 카드를 누르면 해당 사이트만 크롤러 상태 목록에 표시됩니다. 오래된 사이트를 골라 <Link href="/site-collect">사이트 수집</Link>에서 바로 실행하세요.</p>
        </> : <aside className="snapshot-note snapshot-note--unavailable"><strong>마지막 수집 확인 대기</strong><span>저장소에 연결되면 사이트별 마지막 수집일을 계산합니다.</span></aside>}
      </section>
    </div>
  );
}

// What the "기타" bar is made of: sites missing from the crawler list vs. sites
// with a crawler but no country/type, with the largest sites of each part.
function OtherBreakdown({ parts }: Readonly<{ parts: readonly OtherPart[] }>) {
  if (!parts.length) return null;
  const total = parts.reduce((sum, part) => sum + part.documents, 0);
  return <details className="tech-info">
    <summary>기타 {total.toLocaleString("ko-KR")}건은 무엇인가요?</summary>
    <ul>{parts.map((part) => <li key={part.reason}>
      <strong>{part.label}</strong> · {part.sites.toLocaleString("ko-KR")}개 사이트 · {part.documents.toLocaleString("ko-KR")}건
      <br /><small>큰 곳: {part.topSites.map((site) => `${site.name} ${site.documents.toLocaleString("ko-KR")}건`).join(", ")}</small>
    </li>)}</ul>
  </details>;
}

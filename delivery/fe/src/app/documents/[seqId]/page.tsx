import Link from "next/link";
import { notFound } from "next/navigation";
import { getDocumentDetail } from "@/lib/document-detail";
import { pdfMissingReason, textMissingReason } from "@/lib/document-file-state";
import { resolveSourceFileLink, sourceFileLinkLabel } from "@/lib/source-file-link";

export const dynamic = "force-dynamic";

const formatDate = (value: string | null): string => {
  if (!value) return "정보 없음";
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString("ko-KR");
};

const formatBytes = (value: number | null): string => {
  if (!value) return "크기 미측정";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
};

const TextBlock = ({ children, empty }: Readonly<{ children: string | null; empty: string }>) =>
  children ? <div className="long-copy">{children}</div> : <p className="field-empty">{empty}</p>;

export default async function DocumentDetailPage({ params }: Readonly<{ params: Promise<{ seqId: string }> }>) {
  const { seqId } = await params;
  if (!/^\d+$/.test(seqId) || Number(seqId) < 1) notFound();
  const result = await getDocumentDetail(seqId);
  if (!result.ok && result.kind === "not_found") notFound();
  if (!result.ok) return <div className="detail-unavailable"><p className="eyebrow">DOCUMENT DETAIL</p><h1>문서를 불러올 수 없습니다.</h1><p>데이터 서비스 연결 상태를 확인한 뒤 다시 시도해 주세요.</p><Link href="/documents">← 문서 탐색으로</Link></div>;
  const document = result.data;
  const translatedTitle = document.translations.title;
  const translatedDescription = document.translations.description;
  const generatedSummary = document.generated_summary;
  const sourceLink = resolveSourceFileLink(document.source.pdf_url);

  return <article className="archive-detail">
    <Link className="back-link" href="/documents">← 문서 탐색으로</Link>
    <header className="archive-detail-header">
      <div className="document-kicker"><span>{document.classification.country}</span><span>{document.classification.doc_type}</span><span>{document.site.site_name}</span></div>
      <p className="source-language">SOURCE · {document.source.lang === "unknown" ? "언어 미측정" : document.source.lang.toUpperCase()}</p>
      <h1>{document.source.title}</h1>
      {translatedTitle && <div className="translated-title"><span>한국어 번역</span><h2>{translatedTitle.text}</h2></div>}
      <div className="detail-links"><a href={document.source.meta_url} target="_blank" rel="noreferrer">원문 사이트 ↗</a>{document.site.site_url && <a href={document.site.site_url} target="_blank" rel="noreferrer">수집 기관 ↗</a>}<Link href={`/crawlers/${encodeURIComponent(document.site.site_id)}`}>수집기 정보 보기 →</Link><Link href={`/documents?site_id=${encodeURIComponent(document.site.site_id)}`}>이 사이트의 다른 문서 →</Link></div>
    </header>

    <dl className="archive-facts">
      <div><dt>저자</dt><dd>{document.source.authors || "정보 없음"}</dd></div>
      <div><dt>발행처</dt><dd>{document.source.publisher || "정보 없음"}</dd></div>
      <div><dt>저널·간행물</dt><dd>{document.source.journal || "정보 없음"}</dd></div>
      <div><dt>발행일</dt><dd>{formatDate(document.source.published_date)}</dd></div>
      <div><dt>수집일</dt><dd>{formatDate(document.source.collected_at)}</dd></div>
      <div><dt>문서 번호</dt><dd>{document.seq_id.toLocaleString("ko-KR")}</dd></div>
    </dl>

    <section className="detail-file-state" aria-label="문서 파일 상태">
      <div><span className={document.files.has_pdf ? "file-ready" : ""}>PDF {document.files.has_pdf ? "확보" : "미확보"}</span><small>{document.files.has_pdf ? formatBytes(document.files.pdf_size_bytes) : pdfMissingReason(document.files.original_filename)}</small></div>
      <div><span className={document.files.has_text ? "file-ready" : ""}>TEXT {document.files.has_text ? "확보" : "미확보"}</span><small>{document.files.has_text ? "PDF에서 추출한 텍스트가 있습니다." : textMissingReason(document.files.has_pdf)}</small></div>
      <div><span>원본 파일명</span><small>{document.files.original_filename || "파일명 정보 없음"}</small></div>
      <p className="detail-links">{document.files.has_pdf&&<a href={`/api/documents/${document.seq_id}/pdf`} target="_blank">PDF 열기 ↗</a>}{document.files.has_text&&<a href={`/api/documents/${document.seq_id}/text`} target="_blank">추출 텍스트 열기 ↗</a>}{sourceLink&&<a href={sourceLink.href} target="_blank" rel="noreferrer">{sourceFileLinkLabel(sourceLink)}</a>}{!document.files.has_pdf&&!document.files.has_text&&!sourceLink&&"제공 가능한 파일이 없습니다."}</p>
    </section>

    <div className="detail-reading-grid">
      <section className="reading-panel reading-panel--source">
        <div className="reading-heading"><div><p className="eyebrow">ORIGINAL ABSTRACT</p><h2>원문 초록</h2></div><span>{document.source.lang.toUpperCase()}</span></div>
        <TextBlock empty="저장된 원문 초록이 없습니다.">{document.source.abstract}</TextBlock>
      </section>
      <section className="reading-panel reading-panel--translation">
        <div className="reading-heading"><div><p className="eyebrow">KOREAN SUMMARY</p><h2>한국어 핵심 요약</h2></div><span>{document.translations.target_locale}</span></div>
        <TextBlock empty="아직 생성된 한국어 요약이 없습니다.">{generatedSummary?.summary_text ?? translatedDescription?.text ?? null}</TextBlock>
        {generatedSummary?.key_points?.length ? <ul className="summary-points">{generatedSummary.key_points.map(point=><li key={point}>{point}</li>)}</ul>:null}
        {generatedSummary?.institutions?.length ? <p className="summary-institutions"><strong>관련 기관</strong> · {generatedSummary.institutions.join(" · ")}</p>:null}
        {generatedSummary&&<p className={`summary-quality summary-quality--${generatedSummary.quality_decision}`}>{generatedSummary.quality_decision==="auto_approved"?"자동 품질 승인":"검토 권장"} · {generatedSummary.quality_score}점</p>}
        {(translatedTitle || translatedDescription || generatedSummary) && <p className="translation-provenance">저장된 결과 · {generatedSummary?.model_version || translatedDescription?.model_version || translatedTitle?.model_version} · 외부 API를 실시간 호출하지 않음</p>}
      </section>
    </div>

    <section className="detail-secondary">
      <div><p className="eyebrow">KEYWORDS</p><h2>키워드</h2><TextBlock empty="키워드 정보가 없습니다.">{document.source.keywords}</TextBlock></div>
      <div><p className="eyebrow">SUMMARY</p><h2>저장된 요약</h2><TextBlock empty="저장된 요약이 없습니다.">{document.source.summary}</TextBlock>{document.source.summary_model && <small>생성 모델: {document.source.summary_model}</small>}</div>
    </section>
  </article>;
}

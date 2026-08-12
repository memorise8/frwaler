import { TranslationConsole } from "./translation-console";

export default function TranslationsPage() {
  return <div className="translations-page">
    <header className="documents-hero"><div><p className="eyebrow">TRANSLATION OPERATIONS</p><h1>번역 작업을<br />안전하게 운영합니다.</h1></div><p>외부 API와 내부 모델을 같은 작업 계약으로 관리합니다. 문서 원문은 변경하지 않으며 완료된 결과만 상세 화면에 표시됩니다.</p></header>
    <aside className="snapshot-note"><strong>실행 원칙</strong><span>먼저 대상 건수를 확인하고 소량으로 시작하세요. API 키와 endpoint는 서버 환경변수에만 보관됩니다.</span></aside>
    <TranslationConsole />
  </div>;
}

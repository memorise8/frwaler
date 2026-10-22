import { TranslationConsole } from "./translation-console";
import { QualityDashboard } from "./quality-dashboard";
import { BatchSafetyDashboard } from "./batch-safety-dashboard";

// 순서: 1. 등록 → 2. 현황(작업·품질) → 3. 안전장치.
export default function TranslationsPage() {
  return <div className="translations-page">
    <header className="section-heading"><div><h1>번역 작업</h1><p>문서 제목을 번역하고 초록을 한국어로 요약합니다. 원문은 바꾸지 않고, 끝난 결과만 문서 상세 화면에 보입니다.</p></div></header>
    <aside className="snapshot-note"><strong>실행 원칙</strong><span>먼저 대상 건수를 확인하고 적은 양으로 시작하세요. 접속 키와 서버 주소는 서버 설정에만 보관됩니다.</span></aside>
    <TranslationConsole />
    <QualityDashboard />
    <BatchSafetyDashboard />
  </div>;
}

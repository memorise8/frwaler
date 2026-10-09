import type { CollectionScope } from "@/lib/date-plan";

export default function CollectionScopeSummary({ scope }: { scope: CollectionScope }) {
  if (scope.validation_failed) return <p role="alert">범위 기록 검증에 실패했습니다. 계획을 다시 확인해야 합니다.</p>;
  return <div className="snapshot-note">
    <strong>지정 날짜 범위 · {scope.lower} ~ {scope.upper}</strong>
    <p>{scope.completed_at ? "범위 처리 완료" : "범위 처리 미완료"} · 원문 항목 {(scope.items_done ?? 0).toLocaleString("ko-KR")}건 처리</p>
    <p>처리 항목에는 기존 문서와 수집기 제외 항목이 포함됩니다. 사이트 전체 수집·PDF·텍스트 검증은 별도입니다.</p>
  </div>;
}

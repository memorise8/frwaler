import Link from "next/link";

// Next only honours a not-found boundary at the app root here: a not-found.tsx
// placed inside a dynamic segment (crawlers/[siteId], documents/[seqId]) is
// ignored, and adding a layout.tsx to that segment does not activate it either
// — both were measured against this app. So this one page serves every case:
// notFound() thrown by a detail page, and any unmatched URL.
export default function NotFound() {
  return (
    <div className="detail-unavailable">
      <p className="eyebrow">NOT FOUND</p>
      <h1>페이지를 찾을 수 없습니다.</h1>
      <p>주소가 올바른지 확인해 주세요. 수집기 ID나 문서 번호가 잘못된 경우에도 이 화면이 표시됩니다.</p>
      <div className="detail-links">
        <Link href="/crawlers">크롤러 상태 →</Link>
        <Link href="/documents">문서 탐색 →</Link>
      </div>
    </div>
  );
}

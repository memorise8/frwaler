import Link from "next/link";

export default function DocumentNotFound() {
  return <div className="detail-unavailable"><p className="eyebrow">DOCUMENT NOT FOUND</p><h1>문서를 찾을 수 없습니다.</h1><p>문서 번호가 올바른지 확인하거나 검색 결과에서 다시 선택해 주세요.</p><Link href="/documents">← 문서 탐색으로</Link></div>;
}

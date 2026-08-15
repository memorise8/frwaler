import Link from "next/link";

export default function CrawlerNotFound() {
  return <div className="detail-unavailable"><p className="eyebrow">CRAWLER NOT FOUND</p><h1>크롤러를 찾을 수 없습니다.</h1><p>수집기 ID가 올바른지 확인하거나 목록에서 다시 선택해 주세요.</p><Link href="/">← 크롤러 목록으로</Link></div>;
}

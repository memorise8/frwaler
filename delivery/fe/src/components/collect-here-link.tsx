import Link from "next/link";
import { siteCollectHref } from "@/lib/nav-items";

// Collection starts only on 사이트 수집, where the large-run approval check
// lives. The crawler screens keep their read-only views and point here
// instead of offering their own run buttons (RunPanel / BulkRunPanel stay
// for the password-protected job dashboard).
export default function CollectHereLink({ siteId }: Readonly<{ siteId?: string }>) {
  return (
    <section className="run-panel" aria-label="수집 시작">
      <p className="run-note">이 화면은 보기 전용입니다.</p>
      <Link className="run-link" href={siteCollectHref(siteId)}>수집은 사이트 수집에서 시작합니다 →</Link>
    </section>
  );
}

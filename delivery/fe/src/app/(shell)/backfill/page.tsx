import { operatorHeaders } from "@/lib/backend-auth";
import { getCrawlerHealth } from "@/lib/crawler-health";
import { formatSeoul } from "@/lib/seoul-time";
import { backfillStateLabel, formatBackfillPercent, type ProgressRow } from "@/lib/backfill-progress";

// Reads live backfill progress from the BE on every request. Without this
// flag `next build` would prerender the page against a backend that does not
// exist at build time, baking a permanent "연결 안 됨" screen into the image --
// the same failure mode /schedules shipped without it first.
export const dynamic = "force-dynamic";

type ProgressSite = ProgressRow & Readonly<{
  site_id: string;
  items_done: number;
  total_estimate: number | null;
  docs_in_db: number;
  updated_at: string;
}>;

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

async function loadProgress(): Promise<ProgressSite[] | null> {
  try {
    const response = await fetch(`${backendUrl()}/progress`, { headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) return null;
    return ((await response.json()) as { sites: ProgressSite[] }).sites;
  } catch {
    return null;
  }
}

const STATE_BADGE_CLASS: Record<ReturnType<typeof backfillStateLabel>, string> = {
  "시작 전": "verify-tag",
  "대기 중": "verify-tag",
  "진행 중": "material-tag",
  "취소 처리 중": "verify-tag",
  "중단됨": "verify-tag",
  "완주": "verify-tag verify-tag--collected",
};

export default async function BackfillProgress() {
  const rows = await loadProgress();
  const siteNameById = new Map(getCrawlerHealth().map((site) => [site.siteId, site.siteName]));
  // 요약 카드와 행 배지가 같은 분류 규칙을 쓰도록 라이브러리에서 파생한다 --
  // 여기서 다시 분기하면 규칙이 바뀔 때 둘이 조용히 어긋난다.
  const completed = rows?.filter((row) => backfillStateLabel(row) === "완주").length ?? 0;
  const inProgress = rows?.filter((row) => backfillStateLabel(row) === "진행 중").length ?? 0;
  const notStarted = rows?.filter((row) => backfillStateLabel(row) === "시작 전").length ?? 0;
  const stopped = rows?.filter((row) => backfillStateLabel(row) === "중단됨").length ?? 0;
  const waiting = rows?.filter((row) => ["대기 중", "취소 처리 중"].includes(backfillStateLabel(row))).length ?? 0;

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <h1>백필 진행</h1>
          <p className="lede">백필은 사이트에 쌓인 지난 자료를 처음부터 끝까지 모두 받는 작업입니다. 사이트마다 어디까지 받았는지와 예상 문서 수 대비 진행률을 보여 줍니다. 예상 문서 수는 어림값이라 진행률은 100%를 넘지 않게 표시합니다. 시각은 한국 시각입니다.</p>
        </div>
      </header>

      {rows === null ? (
        <aside className="snapshot-note snapshot-note--unavailable">
          <strong>연결 안 됨</strong>
          <span>백필 진행 정보를 불러올 수 없습니다. 잠시 뒤 새로고침해 주세요.</span>
        </aside>
      ) : (
        <>
          <div className="summary-grid">
            <article className="summary-card"><span>전체 사이트</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>백필 기록이 있는 사이트</small></article>
            <article className="summary-card summary-card--good"><span>완주</span><strong>{completed.toLocaleString("ko-KR")}</strong><small>끝까지 다 받음</small></article>
            <article className="summary-card summary-card--secondary"><span>진행 중</span><strong>{inProgress.toLocaleString("ko-KR")}</strong><small>지금 받고 있음</small></article>
            <article className="summary-card"><span>대기·취소 처리 중</span><strong>{waiting.toLocaleString("ko-KR")}</strong><small>차례를 기다리거나 취소하는 중</small></article>
            <article className="summary-card"><span>중단됨</span><strong>{stopped.toLocaleString("ko-KR")}</strong><small>중간까지 받다가 멈춤 (이어 받기 가능)</small></article>
            <article className="summary-card"><span>시작 전</span><strong>{notStarted.toLocaleString("ko-KR")}</strong><small>아직 한 번도 받지 않음</small></article>
          </div>

          <div className="table-shell">
            <table>
              <thead>
                <tr>
                  <th>사이트</th>
                  <th>상태</th>
                  <th className="number">진행률</th>
                  <th className="number">받은 항목</th>
                  <th className="number">저장된 문서</th>
                  <th>마지막 갱신</th>
                  <th>끝난 시각</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const label = backfillStateLabel(row);
                  const percent = formatBackfillPercent(row.items_done, row.total_estimate);
                  return (
                    <tr key={row.site_id}>
                      <td className="site-name">{siteNameById.get(row.site_id) ?? row.site_id}</td>
                      <td><span className={STATE_BADGE_CLASS[label]}>{label}</span></td>
                      <td className="number">{percent ?? "—"}</td>
                      <td className="number">{row.items_done.toLocaleString("ko-KR")}</td>
                      <td className="number">{row.docs_in_db.toLocaleString("ko-KR")}</td>
                      <td>{formatSeoul(row.updated_at)}</td>
                      <td>{formatSeoul(row.completed_at, { fallback: "—" })}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!rows.length && <div className="empty">불러오기는 됐지만 운영 DB 에 백필 진행 기록이 없습니다. 사이트별 수집 진행은 <a href="/site-collect">사이트 수집</a>에서 봅니다.</div>}
          </div>
        </>
      )}
    </div>
  );
}

import { getCrawlerHealth } from "@/lib/crawler-health";
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
    const response = await fetch(`${backendUrl()}/progress`, { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) return null;
    return ((await response.json()) as { sites: ProgressSite[] }).sites;
  } catch {
    return null;
  }
}

const STATE_BADGE_CLASS: Record<ReturnType<typeof backfillStateLabel>, string> = {
  "시작 전": "verify-tag",
  "진행 중": "material-tag",
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

  return (
    <div className="status-page">
      <header className="hero">
        <div>
          <p className="eyebrow">BACKFILL PROGRESS</p>
          <h1>전체 백필 수집의<br />진행 상황을 확인합니다.</h1>
          <p className="lede">사이트별 백필 커서와 예상 대비 진행률을 보여줍니다. 예상 문서 수는 추정치이므로 진행률은 100%를 넘지 않게 표시합니다.</p>
        </div>
      </header>

      {rows === null ? (
        <aside className="snapshot-note snapshot-note--unavailable">
          <strong>연결 안 됨</strong>
          <span>백필 진행 정보를 불러올 수 없습니다.</span>
        </aside>
      ) : (
        <>
          <div className="summary-grid">
            <article className="summary-card"><span>전체 사이트</span><strong>{rows.length.toLocaleString("ko-KR")}</strong><small>백필 기록 있음</small></article>
            <article className="summary-card summary-card--good"><span>완주</span><strong>{completed.toLocaleString("ko-KR")}</strong><small>백필 완료</small></article>
            <article className="summary-card summary-card--secondary"><span>진행 중</span><strong>{inProgress.toLocaleString("ko-KR")}</strong><small>커서 저장됨</small></article>
            <article className="summary-card"><span>시작 전</span><strong>{notStarted.toLocaleString("ko-KR")}</strong><small>아직 커서 없음</small></article>
          </div>

          <div className="table-shell">
            <table>
              <thead>
                <tr>
                  <th>사이트</th>
                  <th>상태</th>
                  <th className="number">진행률</th>
                  <th className="number">수집 완료</th>
                  <th className="number">DB 저장</th>
                  <th>마지막 갱신</th>
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
                      <td>{new Date(row.updated_at).toLocaleString("ko-KR")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!rows.length && <div className="empty">백필 진행 기록이 없습니다.</div>}
          </div>
        </>
      )}
    </div>
  );
}

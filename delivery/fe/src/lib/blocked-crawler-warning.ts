// scripts/audit/crawler_status_final.csv assigns exactly four failure
// categories to unhealthy crawlers: IP차단 (18 sites), 폐쇄/네트워크 (6),
// 수리(코드) (4), 점검필요 (4). Only the first two describe a condition on
// the *target* site (it is blocking or unreachable) that will almost
// certainly reproduce on the very next run; 수리(코드)/점검필요 describe
// something about our own crawler code, which running again says nothing
// new about. Warn only for the categories where "run it again" is a
// concretely bad idea, not for every unhealthy row.
const LIKELY_TO_FAIL_AGAIN: ReadonlySet<string> = new Set(["IP차단", "폐쇄/네트워크"]);

export type HealthState = "healthy" | "unhealthy";

export type CrawlerRiskInfo = Readonly<{
  readonly status: HealthState;
  readonly category: string;
  readonly reason: string;
}>;

// Returns a warning to show before the operator clicks 실행, or null when
// there is nothing to warn about. The button itself must stay enabled --
// the underlying audit verdict is a dated snapshot (see crawler-health.ts)
// that may no longer hold, so an operator retesting after a network change
// is a legitimate use, not a mistake to block.
export const describeBlockedCrawlerWarning = (crawler: CrawlerRiskInfo): string | null => {
  if (crawler.status !== "unhealthy") return null;
  if (!LIKELY_TO_FAIL_AGAIN.has(crawler.category)) return null;
  const reason = crawler.reason.trim();
  const reasonSuffix = reason ? ` (${reason})` : "";
  // "상태로" (not "{category}로") deliberately avoids attaching a Korean
  // particle directly to the variable category text -- "으로" vs "로"
  // depends on whether the preceding syllable has a trailing consonant
  // (IP차단 does, 폐쇄/네트워크 does not), and "상태" always takes "로".
  return `최근 검증에서 '${crawler.category}' 상태로 분류된 크롤러입니다${reasonSuffix}. `
    + "지금 실행해도 같은 이유로 실패할 가능성이 있습니다. 네트워크 조건이 바뀌었다면 계속 진행해 다시 확인할 수 있습니다.";
};

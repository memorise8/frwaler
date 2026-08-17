// Reconciles the delivered audit verdict with what this deployment has actually
// observed. The catalogue's 정상/실패 is a snapshot from one network on one day
// and nothing updates it -- several of its failures literally read "우리 IP
// 차단 ... 클라이언트 egress에서 재확인 필요". This turns a customer's own job
// history into the second half of that sentence.
//
// Neither side overrides the other. The console shows both, because a
// disagreement is information: "the audit said blocked, it works here" is the
// whole reason the customer re-ran it.

export type VerificationRow = Readonly<{
  site_id: string;
  last_status: string;
  last_saved_count: number;
  last_error: string | null;
  last_finished_at: string | null;
  jobs: number;
  best_saved_count: number;
}>;

export type VerificationState =
  // Never run on this deployment -- the audit snapshot is all there is.
  | "unrun"
  // A run here stored documents. The strongest claim available: the crawler
  // reaches its source from this network, whatever the snapshot says.
  | "collected"
  // Ran to completion but added nothing. Deliberately not called success or
  // failure: a re-crawl of already-stored documents and a silently blocked
  // fetch both land here, and only the job's captured output tells them apart.
  | "ran_empty"
  // The worker recorded a failure.
  | "failed";

export const resolveVerificationState = (row: VerificationRow | undefined): VerificationState => {
  if (!row || row.jobs === 0) return "unrun";
  if (row.best_saved_count > 0) return "collected";
  if (row.last_status === "failed") return "failed";
  return "ran_empty";
};

export const VERIFICATION_LABEL: Readonly<Record<VerificationState, string>> = {
  unrun: "실행 기록 없음",
  collected: "여기서 수집 확인",
  ran_empty: "실행됨 · 신규 0건",
  failed: "여기서 실패",
};

// True when this deployment's own evidence contradicts the delivered snapshot.
// Both directions matter: a crawler the audit failed that works here should
// stop being treated as broken, and one the audit passed that fails here is
// the customer's problem right now.
export const contradictsAudit = (auditStatus: string, state: VerificationState): boolean =>
  (auditStatus === "unhealthy" && state === "collected")
  || (auditStatus === "healthy" && state === "failed");

export const describeVerification = (
  auditStatus: string,
  state: VerificationState,
  row: VerificationRow | undefined,
): string => {
  if (state === "unrun") return "이 설치에서 실행한 적이 없습니다. 상태는 납품 시점 검증 결과뿐입니다.";
  const runs = `여기서 ${(row?.jobs ?? 0).toLocaleString("ko-KR")}회 실행`;
  if (state === "collected") {
    const best = (row?.best_saved_count ?? 0).toLocaleString("ko-KR");
    return auditStatus === "unhealthy"
      ? `${runs}, 최대 ${best}건을 저장했습니다. 납품 검증은 실패였으나 이 네트워크에서는 동작합니다.`
      : `${runs}, 최대 ${best}건을 저장했습니다.`;
  }
  if (state === "failed") {
    const error = row?.last_error ? ` (${row.last_error})` : "";
    return auditStatus === "healthy"
      ? `${runs}, 마지막 실행이 실패했습니다${error}. 납품 검증은 정상이었습니다.`
      : `${runs}, 마지막 실행이 실패했습니다${error}.`;
  }
  return `${runs}, 아직 저장된 문서가 없습니다. 차단인지 신규 문서가 없는 것인지는 작업 기록의 크롤러 로그로 확인하세요.`;
};

export const indexVerification = (
  rows: readonly VerificationRow[] | undefined,
): ReadonlyMap<string, VerificationRow> => new Map((rows ?? []).map((row) => [row.site_id, row]));

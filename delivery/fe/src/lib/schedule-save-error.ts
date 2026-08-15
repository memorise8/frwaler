// The route this module backs (src/app/api/schedules/[siteId]/route.ts)
// already resolves its own validation failures (interval_hours, mode,
// limit_n via src/lib/crawl-limit.ts, a non-boolean enabled, an unparsable
// request body) into complete Korean `detail` strings directly, without
// ever calling this module -- those never need translating.
//
// This module exists for what that validation cannot cover: the plain,
// untranslated English `detail` strings the backend itself raises for
// /schedules/{site_id} (be/app.py put_schedule, be/access.py
// require_operator, worker/schedules.py upsert's ValueError), and this
// route's own "the backend didn't answer" case. It mirrors
// src/lib/job-submit-error.ts's role for /jobs, and both this route and
// the schedule-manager.tsx client reuse it, so a backend detail is
// translated exactly once no matter which layer first sees it.
export type BackendErrorBody = Readonly<{ readonly detail?: unknown }>;

// Confirmed against be/app.py (put_schedule), be/access.py
// (require_operator) and worker/schedules.py (upsert): these are the only
// plain-string `detail` values that endpoint can produce that are not
// already Korean by the time they would reach this function.
const KNOWN_DETAIL_MESSAGES: Readonly<Record<string, string>> = {
  "site not found": "등록되지 않은 사이트입니다. 사이트 ID를 다시 확인하세요.",
  "operator authentication required": "운영자 인증에 실패했습니다. 인증 토큰 설정을 확인하세요.",
  "operator authentication unavailable": "운영자 인증 기능을 사용할 수 없습니다. 서버 설정을 확인하세요.",
  "invalid schedule configuration": "예약 설정 값이 올바르지 않습니다. 입력값을 다시 확인하세요.",
};

// A response this route or the backend produced, but with no usable
// `detail` at all -- an empty object, a non-string detail, or a body that
// never parsed as JSON in the first place -- still deserves a message that
// carries the one fact we do have: the status code. It must never
// silently fall back to the single flat sentence this module exists to
// retire.
export const describeScheduleSaveFailure = (status: number, body: BackendErrorBody): string => {
  const { detail } = body;
  if (typeof detail === "string" && detail.trim() !== "") {
    return KNOWN_DETAIL_MESSAGES[detail] ?? detail;
  }
  return `예약을 저장하지 못했습니다 (상태 코드 ${status}).`;
};

// The route's own fetch to the backend can fail outright (connection
// refused, timeout, a non-JSON body it cannot parse at all) with no
// response and therefore no status code to report. This is a distinct,
// fixed message for that one case, not routed through
// describeScheduleSaveFailure, because there is no body to inspect.
export const SCHEDULE_BACKEND_UNREACHABLE_MESSAGE =
  "백엔드 서버에 연결할 수 없습니다. 잠시 후 다시 시도하세요.";

// fetch() itself rejecting (not a bad response -- no response at all)
// means the browser never reached this app's own /api/schedules route, a
// client-side network failure distinct from every case above, all of
// which assume at least one HTTP response was received.
export const SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE =
  "요청을 보낼 수 없습니다. 네트워크 연결을 확인한 뒤 다시 시도하세요.";

// The backend reports a failed /jobs POST in one of two shapes:
//
// 1. A manually raised HTTPException with a plain string `detail`, e.g.
//    {"detail":"site not found"} or {"detail":"site already has an active
//    job"} (be/app.py post_job).
// 2. A FastAPI/Pydantic validation rejection, whose `detail` is instead an
//    array of per-field error objects, e.g.
//    {"detail":[{"type":"less_than_equal","loc":["body","limit_n"],
//    "msg":"Input should be less than or equal to 1000", ...}]}
//    (confirmed against a live 422 from POST /jobs with limit_n=5000).
//
// The previous route only handled shape 1 and, for anything else, discarded
// the body entirely behind a generic "BE 응답 오류 (422)" -- exactly the
// defect this module exists to fix. It must never again drop information
// the backend actually supplied.
export type BackendErrorBody = Readonly<{ readonly detail?: unknown }>;

const KNOWN_DETAIL_MESSAGES: Readonly<Record<string, string>> = {
  "site not found": "등록되지 않은 크롤러 ID입니다. 크롤러 ID를 다시 확인하세요.",
  "site already has an active job":
    "이 크롤러는 이미 대기 중이거나 실행 중인 작업이 있습니다. 작업 현황에서 상태를 확인한 뒤 다시 시도하세요.",
};

type PydanticFieldError = Readonly<{ readonly loc?: unknown; readonly msg?: unknown }>;

const isPydanticFieldError = (value: unknown): value is PydanticFieldError =>
  typeof value === "object" && value !== null && "msg" in value;

const fieldPath = (loc: unknown): string =>
  Array.isArray(loc) ? loc.filter((part) => part !== "body").map(String).join(".") : "";

const describeFieldErrors = (errors: readonly unknown[]): string | null => {
  const parts = errors
    .filter(isPydanticFieldError)
    .map((error) => {
      const field = fieldPath(error.loc);
      const msg = typeof error.msg === "string" ? error.msg : "값이 올바르지 않습니다.";
      return field ? `${field}: ${msg}` : msg;
    })
    .filter((part) => part.length > 0);
  return parts.length > 0 ? parts.join("; ") : null;
};

export const describeJobSubmitFailure = (status: number, body: BackendErrorBody): string => {
  const { detail } = body;
  if (typeof detail === "string" && detail.trim() !== "") {
    return KNOWN_DETAIL_MESSAGES[detail] ?? `백엔드 오류: ${detail} (상태 코드 ${status})`;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const fields = describeFieldErrors(detail);
    return fields
      ? `요청 값이 올바르지 않습니다 (${fields})`
      : `요청 값이 올바르지 않습니다 (상태 코드 ${status})`;
  }
  return `백엔드 응답 오류가 발생했습니다 (상태 코드 ${status})`;
};

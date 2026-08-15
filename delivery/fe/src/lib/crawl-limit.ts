// The backend's JobIn.limit_n is `Field(default=None, ge=1, le=1000)` (be/app.py),
// so a job's save cap must be a positive integer no greater than 1000, or an
// explicit "no cap" that the backend reads as unlimited. Anything missing,
// non-numeric, zero, negative, non-integer, or above that cap must be
// rejected rather than coerced into a default — an absent or unusable limit
// must never be read as "no limit". Only a value equal to the exact sentinel
// below counts as an intentional, unbounded choice; every other absent-input
// shape (undefined, null, "", whitespace) keeps failing closed.
export const MAX_CRAWL_LIMIT = 1000;

// A string that can never arise from a number field being left blank, typed
// into, or coerced (Number("") is 0, Number(undefined) is NaN — neither is
// this string), so it cannot be mistaken for "not provided". It is only ever
// produced by the run panel when the operator actively picks 무제한 수집.
export const UNBOUNDED_CRAWL_LIMIT = "unbounded";

export type CrawlLimitResult =
  | { readonly ok: true; readonly limit: number | null }
  | { readonly ok: false; readonly error: string };

export const parseCrawlLimit = (value: unknown): CrawlLimitResult => {
  if (value === UNBOUNDED_CRAWL_LIMIT) {
    return { ok: true, limit: null };
  }
  if (value === null || value === undefined) {
    return { ok: false, error: "최대 저장 건수를 입력해야 합니다." };
  }
  if (typeof value === "string" && value.trim() === "") {
    return { ok: false, error: "최대 저장 건수를 입력해야 합니다." };
  }
  const parsed = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(parsed)) {
    return { ok: false, error: "최대 저장 건수는 숫자여야 합니다." };
  }
  if (!Number.isInteger(parsed)) {
    return { ok: false, error: "최대 저장 건수는 정수여야 합니다." };
  }
  if (parsed <= 0) {
    return { ok: false, error: "최대 저장 건수는 1 이상이어야 합니다." };
  }
  if (parsed > MAX_CRAWL_LIMIT) {
    return { ok: false, error: `최대 저장 건수는 ${MAX_CRAWL_LIMIT}건을 넘을 수 없습니다.` };
  }
  return { ok: true, limit: parsed };
};

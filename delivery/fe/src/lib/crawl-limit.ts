// The backend's JobIn.limit_n is `Field(default=None, ge=1, le=1000)` (be/app.py),
// so a job's save cap must be a positive integer no greater than 1000. Anything
// missing, non-numeric, zero, negative, non-integer, or above that cap must be
// rejected rather than coerced into a default — an absent or unusable limit
// must never be read as "no limit".
export const MAX_CRAWL_LIMIT = 1000;

export type CrawlLimitResult =
  | { readonly ok: true; readonly limit: number }
  | { readonly ok: false; readonly error: string };

export const parseCrawlLimit = (value: unknown): CrawlLimitResult => {
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

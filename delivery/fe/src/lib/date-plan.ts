export type DatePlan = {
  lower: string; upper: string;
  ranges: { lower: string; upper: string; expected_count: number }[];
  total: number;
};

function day(value: unknown): string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("날짜 형식이 올바르지 않습니다.");
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) throw new Error("유효하지 않은 날짜입니다.");
  return value;
}

export function readDatePlan(value: unknown): DatePlan {
  const data = value as { state?: unknown; bounds?: { oldest?: { publication_date?: unknown }; newest?: { publication_date?: unknown } }; leaves?: unknown; reported_leaf_count_sum?: unknown };
  if (!data || data.state !== "disjoint_date_range_counts_observed" || !Array.isArray(data.leaves) || data.leaves.length < 1 || data.leaves.length > 10000) throw new Error("날짜별 총량 관측이 끝난 계획 파일이 필요합니다.");
  const lower = day(data.bounds?.oldest?.publication_date), upper = day(data.bounds?.newest?.publication_date);
  let next = lower, total = 0;
  const ranges = [...data.leaves].sort((a, b) => String(a?.lower).localeCompare(String(b?.lower))).map((entry: unknown) => {
    const row = entry as { lower?: unknown; upper?: unknown; reported_count?: unknown };
    if (!row) throw new Error("구간 정보가 없습니다.");
    const lo = day(row.lower), hi = day(row.upper), count = row.reported_count;
    if (lo !== next || lo > hi || typeof count !== "number" || !Number.isInteger(count) || count < 0 || count >= 1500) throw new Error("날짜 구간이 겹치거나 빠져 있거나, 구간 건수가 유효하지 않습니다.");
    next = new Date(new Date(`${hi}T00:00:00Z`).getTime() + 86400000).toISOString().slice(0, 10);
    total += count;
    return { lower: lo, upper: hi, expected_count: count };
  });
  if (ranges.at(-1)?.upper !== upper || total !== data.reported_leaf_count_sum) throw new Error("관측 범위 또는 합계가 일치하지 않습니다.");
  return { lower, upper, ranges, total };
}

export type CollectionScope = {
  scope_id: string; lower?: string; upper?: string; observation_id?: string;
  items_done?: number; completed_at?: string | null; validation_failed?: boolean;
  whole_site_completion: false; files_verified: false;
};

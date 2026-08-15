// Mirrors the `freshness_bucket` values GET /freshness reports per site
// (within_7_days | 8_to_30_days | 31_to_90_days | over_90_days | never), plus
// the one FE-only value that stands in for the combined "90일 초과·미수집"
// card, which spans both over_90_days and never in a single filter click.
export const OVER_90_OR_NEVER = "over_90_or_never";

export type FreshnessFilterValue =
  | "within_7_days"
  | "8_to_30_days"
  | "31_to_90_days"
  | typeof OVER_90_OR_NEVER;

export const FRESHNESS_FILTER_VALUES: readonly FreshnessFilterValue[] = [
  "within_7_days",
  "8_to_30_days",
  "31_to_90_days",
  OVER_90_OR_NEVER,
];

// Pure predicate: does a site's raw freshness_bucket belong under the
// selected catalogue filter value? Kept separate from any fetch/render code
// so it can be unit tested without a backend or a DOM.
export const matchesFreshnessFilter = (bucket: string, filter: string): boolean => {
  if (filter === OVER_90_OR_NEVER) return bucket === "over_90_days" || bucket === "never";
  return bucket === filter;
};

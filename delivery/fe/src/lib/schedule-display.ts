// Plain-Korean wording and edit bookkeeping for the 수집 예약 screen.

export const SCHEDULE_INTERVAL_OPTIONS: readonly Readonly<{ hours: number; label: string }>[] = [
  { hours: 24, label: "매일" },
  { hours: 168, label: "매주(7일)" },
  { hours: 720, label: "매월(30일)" },
];

/** 24 → "매일", 168 → "매주(7일)", 48 → "2일마다", 6 → "6시간마다". */
export const formatScheduleInterval = (hours: number): string => {
  const known = SCHEDULE_INTERVAL_OPTIONS.find((option) => option.hours === hours);
  if (known) return known.label;
  if (!Number.isFinite(hours) || hours <= 0) return "알 수 없음";
  return hours % 24 === 0 ? `${hours / 24}일마다` : `${hours}시간마다`;
};

const MODE_LABEL: Readonly<Record<string, string>> = {
  incremental: "증분(새 글만)",
  full: "전체 다시 수집",
  enum_incremental: "새 수집기 증분(새 글만)",
};

export const formatScheduleMode = (mode: string): string => MODE_LABEL[mode] ?? mode;

/** Modes the form offers. enum_incremental runs the plan-driven collector through the collect runner. */
export const SCHEDULE_MODE_OPTIONS: readonly Readonly<{ mode: string; label: string }>[] = [
  { mode: "incremental", label: "증분(새 글만) — 등록 수집기" },
  { mode: "enum_incremental", label: "새 수집기 증분(새 글만) — 수집 실행기" },
];

/**
 * enum_incremental is bounded by the plan (known_run / max_pages), never by a count, and is
 * not an unverified registered crawler: it takes no 1회 제한 and no trial confirmation.
 */
export const isRunnerScheduleMode = (mode: string): boolean => mode === "enum_incremental";

export const scheduleCreateBody = (mode: string, intervalHours: number, limit: number | null) => ({
  interval_hours: intervalHours,
  mode,
  limit_n: isRunnerScheduleMode(mode) ? null : limit,
  enabled: true,
});

export const needsTrialConfirm = (body: { enabled?: boolean; mode?: string }): boolean =>
  Boolean(body.enabled) && !isRunnerScheduleMode(body.mode ?? "incremental");

export type ScheduleRowState = Readonly<{
  site_id: string;
  interval_hours: number;
  mode: string;
  limit_n: number | null;
  enabled: boolean;
}>;

/** site_id → the 사용 checkbox value the operator has chosen but not yet saved. */
export type ScheduleDrafts = Readonly<Record<string, boolean>>;

/**
 * Records a checkbox change. Ticking a row back to its saved value drops the
 * draft, so the row is no longer dirty and the save button has nothing to do.
 */
export const setScheduleDraft = (drafts: ScheduleDrafts, row: ScheduleRowState, enabled: boolean): ScheduleDrafts => {
  const next = { ...drafts };
  if (enabled === row.enabled) delete next[row.site_id];
  else next[row.site_id] = enabled;
  return next;
};

export const isScheduleDirty = (drafts: ScheduleDrafts, row: ScheduleRowState): boolean =>
  row.site_id in drafts && drafts[row.site_id] !== row.enabled;

/** The checkbox value to show: the unsaved choice if there is one, else the saved value. */
export const shownEnabled = (drafts: ScheduleDrafts, row: ScheduleRowState): boolean =>
  row.site_id in drafts ? drafts[row.site_id]! : row.enabled;

/** One PUT body per dirty row: everything as saved, except the new 사용 value. */
export const pendingScheduleSaves = (
  rows: readonly ScheduleRowState[],
  drafts: ScheduleDrafts,
): { siteId: string; body: { interval_hours: number; mode: string; limit_n: number | null; enabled: boolean } }[] =>
  rows.filter((row) => isScheduleDirty(drafts, row)).map((row) => ({
    siteId: row.site_id,
    body: { interval_hours: row.interval_hours, mode: row.mode, limit_n: row.limit_n, enabled: drafts[row.site_id]! },
  }));

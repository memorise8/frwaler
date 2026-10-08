export type ProgressRow = Readonly<{
  cursor: Record<string, number> | null;
  completed_at: string | null;
  active_status?: string | null;
}>;

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

export const formatBackfillPercent = (
  itemsDone: number,
  totalEstimate: number | null | undefined,
): string | null => {
  if (!isCount(itemsDone) || !isCount(totalEstimate ?? NaN) || !totalEstimate) return null;
  const ratio = Math.min(itemsDone / totalEstimate, 1);
  if (ratio === 1) return "100%";
  return `${(ratio * 100).toFixed(1)}%`;
};

export type BackfillState = "시작 전" | "대기 중" | "진행 중" | "취소 처리 중" | "중단됨" | "완주";

export const backfillStateLabel = (row: ProgressRow): BackfillState => {
  if (row.active_status === "cancelling") return "취소 처리 중";
  if (row.active_status === "running") return "진행 중";
  if (row.active_status === "queued") return "대기 중";
  if (row.completed_at) return "완주";
  if (row.cursor) return "중단됨";
  return "시작 전";
};

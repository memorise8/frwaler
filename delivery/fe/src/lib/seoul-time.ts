// 화면에 보이는 시각은 모두 Asia/Seoul 로 고정한다(schedule-time.ts 와 같은 이유: 서버(UTC)와 브라우저(KST)가
// 같은 문자열을 만들어야 hydration 이 깨지지 않고, 운영자는 늘 한국 시각으로 읽는다).
const ZONE = "Asia/Seoul";
const PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

const parse = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined || value === "") return null;
  // "2026-10-20 01:02:03+00" (Postgres) 도 받는다.
  const date = value instanceof Date ? value : new Date(typeof value === "string" ? value.replace(" ", "T").replace(/([+-]\d{2})$/, "$1:00") : value);
  return Number.isNaN(date.getTime()) ? null : date;
};

/** "2026-10-22 14:05" (Asia/Seoul). seconds: true 면 "2026-10-22 14:05:09". 못 읽으면 fallback. */
export const formatSeoul = (value: string | number | Date | null | undefined,
  { seconds = false, fallback = "-" }: Readonly<{ seconds?: boolean; fallback?: string }> = {}): string => {
  const date = parse(value);
  if (!date) return fallback;
  const p = Object.fromEntries(PARTS.formatToParts(date).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}${seconds ? `:${p.second}` : ""}`;
};

/** 날짜만: "2026-10-22" (Asia/Seoul). */
export const formatSeoulDate = (value: string | number | Date | null | undefined, fallback = "-"): string => {
  const full = formatSeoul(value, { fallback: "" });
  return full ? full.slice(0, 10) : fallback;
};

export const SEOUL_LABEL = "한국 시각";

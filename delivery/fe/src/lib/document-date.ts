/** Source dates must not be guessed by JavaScript's locale-dependent parser. */
export function documentDate(value: string | null, raw?: string | null,
  status?: string | null): string {
  if (value && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const parsed = new Date(`${value}T00:00:00Z`);
    if (!Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value) return value;
  }
  const original = raw || value;
  if (!original) return "날짜 미상";
  return `${original} (${status === "partial" ? "부분 날짜" : "날짜 확인 필요"})`;
}

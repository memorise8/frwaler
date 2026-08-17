// How long a crawl job actually took. Until the worker switched these columns
// from now() to clock_timestamp(), every job that saved nothing reported the
// same instant for both -- PostgreSQL's now() is the transaction timestamp, and
// run_job holds one transaction for the whole crawl when there is no save to
// commit. A 3m54s blocked crawl was indistinguishable from an instant one.
//
// Returns null when the job has not finished, or when either stamp is missing
// or unparseable, so the caller can print "-" rather than invent a number.
export const formatJobDuration = (
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string | null => {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const ms = end - start;
  if (ms < 0) return null;
  // Byte-identical stamps mean the measurement collapsed rather than the job
  // being instant: that is the exact signature of the transaction-timestamp
  // bug, and every job recorded before the fix carries it. Two clock_timestamp()
  // readings around real work do not land on the same millisecond. Reporting
  // those as "1초 미만" would repeat the original lie about a 3m54s crawl, so
  // they read as unmeasured -- at the cost of also labelling a genuinely
  // instantaneous run that way, which is the far cheaper mistake.
  if (ms === 0) return null;
  // A sub-second run is a real measurement, not a missing one, and must not
  // print as "0초" -- that is exactly the reading the old bug produced.
  if (ms < 1000) return "1초 미만";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}초`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const rest = seconds % 60;
    return rest ? `${minutes}분 ${rest}초` : `${minutes}분`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours}시간 ${restMinutes}분` : `${hours}시간`;
};

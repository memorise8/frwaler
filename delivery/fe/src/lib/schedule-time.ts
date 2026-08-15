// schedule-manager.tsx is a Client Component whose initial rows are
// rendered once on the server (page.tsx fetches them) and then hydrated in
// the browser. Date#toLocaleString without an explicit timeZone resolves
// against whichever machine's clock is running the call -- the server
// container (UTC in this deployment) during SSR, and the operator's own
// machine (KST) during hydration -- so the very same instant renders two
// different strings and React throws away the server-rendered markup.
//
// Pinning an explicit timeZone here is what actually closes that gap: the
// formatted string becomes identical no matter which machine's local clock
// produced it, because the host clock is no longer part of the
// computation. Deferring the formatted value to a client-only effect was
// considered and rejected -- it would trade the mismatch for a guaranteed
// flash of placeholder text on every single page load, for every operator,
// forever, which is worse than the mismatch it avoids. Rendering the raw
// ISO string was also rejected -- it satisfies hydration but fails the
// actual requirement, which is that the operator can read the time.
//
// The deployment is single-customer and Korean-operated, so "the
// operator's local time" is not ambient to the deployment target -- it is
// always Asia/Seoul, which is what makes pinning a single fixed zone (as
// opposed to, say, a zone read from a request header) both correct and
// sufficient here.
const SCHEDULE_TIME_ZONE = "Asia/Seoul";

export const formatScheduleTimestamp = (iso: string): string => {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "알 수 없음";
  }
  return date.toLocaleString("ko-KR", { timeZone: SCHEDULE_TIME_ZONE });
};

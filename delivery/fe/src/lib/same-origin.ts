// 실행기에 작업을 거는 프록시(POST)를 다른 사이트의 페이지가 몰래 부르지 못하게 막는다.
// 운영자 토큰은 서버 쪽에서 붙으므로, 브라우저가 보낸 요청이 이 화면에서 나왔는지는
// 여기서 확인해야 한다. text/plain 같은 "단순 요청"은 사전 확인 없이 교차 출처로
// 보낼 수 있어서 JSON 본문만 받는다.

const hostOf = (request: Request): string =>
  request.headers.get("x-forwarded-host")?.split(",")[0]?.trim() || request.headers.get("host") || new URL(request.url).host;

const originHost = (origin: string | null): string | null => {
  if (!origin) return null;
  try { return new URL(origin).host; } catch { return null; }
};

export const sameOriginJsonRequest = (request: Request): boolean => {
  const contentType = (request.headers.get("content-type") ?? "").split(";")[0]!.trim().toLowerCase();
  if (contentType !== "application/json") return false;
  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site");
  if (origin !== null && originHost(origin) !== hostOf(request)) return false;
  if (fetchSite !== null) return fetchSite === "same-origin";
  // Sec-Fetch-Site 를 보내지 않는 브라우저는 Origin 이 이 호스트와 같을 때만 받는다.
  return origin !== null;
};

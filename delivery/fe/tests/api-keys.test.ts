import { afterEach, describe, expect, it, vi } from "vitest";
import { configuredLabel, describeApiKeyFailure, lastTestLabel, usable, type ApiKeyRow } from "../src/lib/api-keys";

vi.mock("server-only", () => ({}));

const { POST } = await import("../src/app/api/settings/api-keys/[provider]/route");

const KEY = "secret_key_1234567890";
const row = (over: Partial<ApiKeyRow> = {}): ApiKeyRow => ({
  provider: "openalex", label: "OpenAlex", testable: true, test_method: "works 1건", configured: false, last_test: null, ...over,
});

const request = (body: string, headers: Record<string, string> = {}) => new Request("http://console.test/api/settings/api-keys/openalex", {
  method: "POST",
  headers: { "content-type": "application/json", origin: "http://console.test", host: "console.test", "sec-fetch-site": "same-origin", ...headers },
  body,
});
const params = (provider = "openalex") => ({ params: Promise.resolve({ provider }) });

describe("api key labels", () => {
  it("names each failure the way the operator is asked to act", () => {
    expect(describeApiKeyFailure(422, { reason: "bad_key" })).toBe("키가 맞지 않습니다. 다시 입력해 주세요.");
    expect(describeApiKeyFailure(429, { reason: "rate_limited" })).toContain("한도 초과");
    expect(describeApiKeyFailure(429, { reason: "too_many_attempts" })).toContain("10분 뒤");
    expect(describeApiKeyFailure(502, { reason: "connect_failed" })).toContain("연결 실패");
    expect(describeApiKeyFailure(500, {})).toContain("저장하지 못했습니다");
  });

  it("shows configured state and the last test without any key", () => {
    expect(configuredLabel(row())).toBe("없음");
    expect(configuredLabel(row({ configured: null }))).toBe("알 수 없음");
    const saved = row({ configured: true, last_test: { result: "saved", reason: null, at: "2026-10-27T00:00:00Z" } });
    expect([configuredLabel(saved), lastTestLabel(saved), usable(saved)]).toEqual(["설정됨", "정상 · 저장됨", true]);
    const failed = row({ configured: true, last_test: { result: "bad_key", reason: "authentication_required", at: "2026-10-27T00:00:00Z" } });
    expect([lastTestLabel(failed), usable(failed)]).toEqual(["실패 · 키가 맞지 않음", false]);
    expect(lastTestLabel(row({ testable: false }))).toBe("시험 준비 중");
  });
});

describe("POST /api/settings/api-keys/[provider]", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("refuses a request from another site before anything is sent", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(request(JSON.stringify({ key: KEY }), { origin: "http://evil.test", "sec-fetch-site": "cross-site" }), params());
    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses oversized bodies, bad providers and non-string keys", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await POST(request(JSON.stringify({ key: "x".repeat(3000) })), params())).status).toBe(413);
    expect((await POST(request(JSON.stringify({ key: KEY })), params("../x"))).status).toBe(404);
    expect((await POST(request(JSON.stringify({ key: 5 })), params())).status).toBe(422);
    expect((await POST(request("not json"), params())).status).toBe(422);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the key to the backend only and never answers with it", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ reason: "bad_key", detail: "x", echoed: KEY }), { status: 422 }));
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(request(JSON.stringify({ key: KEY })), params());
    expect(response.status).toBe(422);
    const text = await response.text();
    expect(text).not.toContain(KEY);
    expect(JSON.parse(text).reason).toBe("bad_key");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/settings\/api-keys\/openalex$/);
    expect(url).not.toContain(KEY);
    expect(JSON.parse(init.body as string)).toEqual({ key: KEY });
  });

  it("reports an unreachable backend as a connection failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED " + KEY); }));
    const response = await POST(request(JSON.stringify({ key: KEY })), params());
    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain(KEY);
  });
});

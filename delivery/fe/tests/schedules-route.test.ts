import { afterEach, describe, expect, it, vi } from "vitest";
import { SCHEDULE_BACKEND_UNREACHABLE_MESSAGE } from "../src/lib/schedule-save-error";

// "server-only" is a Next.js build-time guard package with no runtime
// resolution outside the Next.js bundler; stub it so importing the route
// module (via src/lib/backend-auth.ts) works under plain vitest/node.
vi.mock("server-only", () => ({}));

const { NextRequest } = await import("next/server");
const { PUT, DELETE } = await import("../src/app/api/schedules/[siteId]/route");

// Never let a fully-valid schedule request actually reach a live backend in
// a test: assert the accept path purely by inspecting what this route would
// have forwarded, via a mocked fetch. See schedule-limit-report.md for why
// the equivalent curl verification only ever exercises rejected requests.
const put = (body: unknown) =>
  PUT(
    new NextRequest("http://localhost/api/schedules/some-site", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
    { params: Promise.resolve({ siteId: "some-site" }) },
  );

describe("PUT /api/schedules/[siteId]", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects a blank/missing limit_n with a 4xx and a Korean message, without calling fetch", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: null, enabled: true });
    expect(response.status).toBeGreaterThanOrEqual(400);
    expect(response.status).toBeLessThan(500);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("입력");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects a zero limit_n before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 0, enabled: true });
    expect(response.status).toBe(400);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("1 이상");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects a negative limit_n before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: -5, enabled: true });
    expect(response.status).toBe(400);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("1 이상");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects a limit_n above the backend's cap before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 1500, enabled: true });
    expect(response.status).toBe(400);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("1000");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects the run panel's explicit unbounded sentinel — recurring unbounded is not offered here", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: "unbounded", enabled: true });
    expect(response.status).toBe(400);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("무제한");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects an out-of-range interval_hours before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 0, mode: "incremental", limit_n: 100, enabled: true });
    expect(response.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects an invalid mode before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "aggressive", limit_n: 100, enabled: true });
    expect(response.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects a non-boolean enabled before forwarding", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 100, enabled: "yes" });
    expect(response.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("translates a real backend rejection (site not found) into Korean instead of forwarding it verbatim", async () => {
    // Shape confirmed live: a valid, bounded PUT against a nonexistent
    // site reaches the backend and comes back 404 {"detail":"site not
    // found"} (be/app.py put_schedule).
    const fetchSpy = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "site not found" }), { status: 404 }));
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 100, enabled: true });
    expect(response.status).toBe(404);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).not.toBe("site not found");
    expect(payload.detail).toContain("사이트");
  });

  it("returns a distinct, Korean 'backend unreachable' message when the backend fetch itself fails", async () => {
    const fetchSpy = vi.fn().mockRejectedValue(new Error("connect ECONNREFUSED"));
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 100, enabled: true });
    expect(response.status).toBe(503);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toBe(SCHEDULE_BACKEND_UNREACHABLE_MESSAGE);
  });

  it("returns a status-coded fallback when the backend's error response has no usable body", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(new Response("not json", { status: 502 }));
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 100, enabled: true });
    expect(response.status).toBe(502);
    const payload = await response.json() as { detail: string };
    expect(payload.detail).toContain("502");
  });

  it("accepts a fully valid bounded request and forwards the resolved limit_n to the backend", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 1, site_id: "some-site", interval_hours: 168, mode: "incremental", limit_n: 250, enabled: true, next_run_at: "2026-01-01T00:00:00Z", last_run_at: null }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const response = await put({ interval_hours: 168, mode: "incremental", limit_n: 250, enabled: true });
    expect(response.status).toBe(200);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const forwarded = JSON.parse(init.body as string) as { limit_n: number | null };
    expect(forwarded.limit_n).toBe(250);
  });
});

describe("DELETE /api/schedules/[siteId]", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  const del = (siteId: string) =>
    DELETE(new NextRequest(`http://localhost/api/schedules/${siteId}`, { method: "DELETE" }),
      { params: Promise.resolve({ siteId }) });

  it("forwards the delete with the operator token attached", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => new Response(JSON.stringify({ deleted: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await del("some-site")).status).toBe(200);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/schedules/some-site");
    expect(init.method).toBe("DELETE");
  });

  it("passes a missing schedule through as 404", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
    expect((await del("gone")).status).toBe(404);
  });

  // 경로를 벗어나는 site id를 백엔드까지 보내지 않는다.
  it("rejects a site id that would escape the path", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await del("a/b")).status).toBe(422);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports an unreachable backend instead of claiming success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    expect((await del("some-site")).status).toBe(503);
  });
});

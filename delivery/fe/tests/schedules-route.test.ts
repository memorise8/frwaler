import { afterEach, describe, expect, it, vi } from "vitest";

// "server-only" is a Next.js build-time guard package with no runtime
// resolution outside the Next.js bundler; stub it so importing the route
// module (via src/lib/backend-auth.ts) works under plain vitest/node.
vi.mock("server-only", () => ({}));

const { NextRequest } = await import("next/server");
const { PUT } = await import("../src/app/api/schedules/[siteId]/route");

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

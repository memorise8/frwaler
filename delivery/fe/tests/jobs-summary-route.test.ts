import { afterEach, describe, expect, it, vi } from "vitest";

// "server-only" is a Next.js build-time guard with no runtime resolution
// outside the bundler; stub it so the route module imports under plain vitest.
vi.mock("server-only", () => ({}));

const { GET } = await import("../src/app/api/jobs/summary/route");

const summary = { counts: { queued: 3, running: 1, cancelling: 0, done: 0, failed: 0, cancelled: 0 },
  active: 4, finished_24h: 0, avg_seconds: null, samples: 0 };

describe("GET /api/jobs/summary", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("passes the backend summary through untouched", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(summary), { status: 200 })));
    const response = await GET();
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(summary);
  });

  it("forwards a backend failure status rather than inventing a summary", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    expect((await GET()).status).toBe(500);
  });

  // 진행 표시줄이 사라지는 것과 "0건 남음"이라고 말하는 것은 완전히 다르다.
  it("reports a connection failure instead of a zeroed queue", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    const response = await GET();
    expect(response.status).toBe(503);
    expect((await response.json() as { error: string }).error).toContain("진행 상황");
  });
});

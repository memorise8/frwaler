import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { readDatePlan } from "../src/lib/date-plan";
import CollectionScopeSummary from "../src/components/collection-scope";

vi.mock("server-only", () => ({}));
const { POST } = await import("../src/app/api/jobs/date-plan/route");
const plan = { state: "disjoint_date_range_counts_observed", reported_leaf_count_sum: 15,
  bounds: { oldest: { publication_date: "2020-01-01" }, newest: { publication_date: "2020-01-31" } },
  leaves: [{ lower: "2020-01-16", upper: "2020-01-31", reported_count: 5 },
    { lower: "2020-01-01", upper: "2020-01-15", reported_count: 10 }] };

describe("observed date plan import", () => {
  it("sorts independent leaves and preserves source counts", () => {
    const result = readDatePlan(plan);
    expect(result.total).toBe(15); expect(result.ranges[0].lower).toBe("2020-01-01");
    expect(result.ranges[1].expected_count).toBe(5);
  });
  it.each([null, {}, { ...plan, state: "running" }, { ...plan, reported_leaf_count_sum: 14 },
    { ...plan, leaves: [{ lower: "2020-01-01", upper: "2020-01-31", reported_count: null }] },
    { ...plan, leaves: [{ lower: "2020-01-01", upper: "2020-02-30", reported_count: 15 }] },
    { ...plan, leaves: [{ lower: "2020-01-01", upper: "2020-01-31", reported_count: 1500 }] },
    { ...plan, leaves: [...plan.leaves, plan.leaves[0]] },
    { ...plan, leaves: [{ ...plan.leaves[0], lower: "2020-01-17" }, plan.leaves[1]] }
  ])("rejects incomplete, inconsistent or noncontiguous evidence: %j", value => {
    expect(() => readDatePlan(value)).toThrow();
  });
});

describe("date scope display", () => {
  it("keeps scope completion separate from whole-site and file completeness", () => {
    const html = renderToStaticMarkup(createElement(CollectionScopeSummary, { scope: { scope_id: "scope", lower: "2020-01-01", upper: "2020-01-31",
      items_done: 15, completed_at: "2026-10-09", whole_site_completion: false, files_verified: false } }));
    expect(html).toContain("범위 처리 완료"); expect(html).toContain("2020-01-31");
    expect(html).toContain("기존 문서와 수집기 제외 항목"); expect(html).toContain("사이트 전체 수집·PDF·텍스트 검증은 별도");
  });
  it("shows invalid evidence without presenting a successful scope", () => {
    const html = renderToStaticMarkup(createElement(CollectionScopeSummary, { scope: { scope_id: "bad", validation_failed: true, whole_site_completion: false, files_verified: false } }));
    expect(html).toContain("검증에 실패"); expect(html).not.toContain("범위 처리 완료");
  });
});

describe("date plan proxy", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("forwards observation and ranges without silently changing the scope", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 42, scope: { scope_id: "scope" } }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const body = { site_id: "federalregister-gov-documents", observation_id: "observed", ranges: readDatePlan(plan).ranges, trial: true };
    const result = await POST(new Request("http://localhost/api/jobs/date-plan", { method: "POST", body: JSON.stringify(body) }));
    expect(result.status).toBe(201); expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(body);
    expect((await result.json()).id).toBe(42);
  });
  it("preserves a backend conflict and reports connection failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 409 })));
    expect((await POST(new Request("http://localhost", { method: "POST", body: "{}" }))).status).toBe(409);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connection refused")));
    expect((await POST(new Request("http://localhost", { method: "POST", body: "{}" }))).status).toBe(503);
  });
  it("rejects invalid JSON before contacting the backend", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await POST(new Request("http://localhost", { method: "POST", body: "broken" }))).status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

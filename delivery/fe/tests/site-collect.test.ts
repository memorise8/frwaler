import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => undefined }) }));
import {
  describeRejection, filterSites, needsApproval, normalizeRunnerHealth, normalizeSitesReport, normalizeUncollected, performAction,
  REJECTION_LABELS, UNCLASSIFIED, type CollectSite,
} from "@/lib/site-collect";
import SiteCollectConsole from "@/app/(shell)/site-collect/site-collect-console";
import { buildConsole } from "@/lib/collect-console";
import { POST as postRun } from "@/app/api/collect/runs/route";
import { POST as postCancel } from "@/app/api/collect/runs/[runId]/cancel/route";

const SAME_ORIGIN = { "content-type": "application/json", origin: "http://localhost", "sec-fetch-site": "same-origin" };
const runRequest = (body: unknown, headers: Record<string, string> = SAME_ORIGIN) =>
  new Request("http://localhost/api/collect/runs", { method: "POST", headers, body: JSON.stringify(body) });

afterEach(() => vi.unstubAllGlobals());

const report = normalizeSitesReport({
  runner_ok: true,
  status_dir_ok: true,
  sites: [
    { site_id: "ntrs-nasa-gov-search", state: "ready", class: "run", detail: "plan 통과", reason_codes: [], lane: "lane-a", doc_count: 1200, expected: 648013,
      latest_run: { run_id: "r1", mode: "resume", status: "running", started: "2026-10-20T01:00:00Z", progress: { saved: 1200, total: 648013 } } },
    { site_id: "sonar-ch-global", state: "needs_dev", class: "dev", detail: "목록 API 변경", reason_codes: ["needs_dev"], lane: "lane-b", doc_count: 0, latest_run: null },
    { site_id: "e-stat-go-jp-stat-search", state: "", class: "run", detail: "", reason_codes: ["api:stat"], lane: "lane-a", latest_run: null,
      plan: { ready: true, expected_items: 1721215, approved_large: false } },
    { site_id: "blocked-example", state: "blocked_access", class: "nego", detail: "robots 금지", lane: "lane-b" },
    { site_id: "fresh-site", class: "" },
    { detail: "site_id 없는 행은 버린다" },
  ],
});
const sites = report.sites;
const ids = (rows: readonly CollectSite[]) => rows.map((row) => row.site_id);

describe("normalizeSitesReport", () => {
  it("keeps rows with a site id and tolerates missing fields", () => {
    expect(ids(sites)).toEqual(["ntrs-nasa-gov-search", "sonar-ch-global", "e-stat-go-jp-stat-search", "blocked-example", "fresh-site"]);
    const fresh = sites[4]!;
    expect(fresh).toMatchObject({ class: "", state: "", lane: "", doc_count: null, expected: null, latest_run: null, reason_codes: [] });
    expect(sites[0]!.latest_run).toMatchObject({ run_id: "r1", status: "running", done: 1200, total: 648013 });
    expect(report.runner_ok).toBe(true);
  });

  it("treats an unexpected body as empty and the runner as down", () => {
    expect(normalizeSitesReport(null)).toEqual({ sites: [], runner_ok: false, status_dir_ok: false, progress_ok: null });
    expect(normalizeSitesReport([{ site_id: "a" }]).sites).toHaveLength(1);
  });
});

describe("draft BE /collect shapes", () => {
  it("reads documents, status_file, plan and status_source", () => {
    const draft = normalizeSitesReport({
      status_source: { ok: true, reason: null, files: 3 }, runner_ok: false, runner_reason: "runner_unavailable",
      sites: [{ site_id: "x-site", state: null, class: "dev", documents: 7, status_file: "lane-c.csv",
        plan: { ready: false, why: "probe 미통과", expected_items: 900 }, latest_run: { run_id: "r0001", status: "failed", started_at: "2026-10-19T00:00:00Z" } }],
    });
    expect(draft.status_dir_ok).toBe(true);
    expect(draft.runner_ok).toBe(false);
    expect(draft.sites[0]).toMatchObject({ doc_count: 7, file: "lane-c.csv", expected: 900, plan: { ready: false, why: "probe 미통과" } });
    expect(draft.sites[0]!.latest_run).toMatchObject({ run_id: "r0001", started: "2026-10-19T00:00:00Z" });
  });

  it("turns uncollected_reasons codes into a Korean reason", () => {
    const [row] = normalizeUncollected({ sites: [{ site_id: "x-site", class: "nego", note: null, uncollected_reasons: ["class_nego", "no_documents"] }] });
    expect(row).toEqual({ site_id: "x-site", class: "nego", reason: "협의 필요 · 수집 문서 0건", note: "" });
  });

  it("derives lane from status_file and run progress from result.saved", () => {
    const [site] = normalizeSitesReport({ sites: [{ site_id: "y", status_file: "L2a.csv",
      latest_run: { run_id: "r0002", status: "done", result: { site_status: "complete", stop_reason: null, saved: 321, failed: 2 } } }] }).sites;
    expect(site!.lane).toBe("L2a");
    expect(site!.latest_run).toMatchObject({ done: 321, result: "complete" });
    const [failed] = normalizeSitesReport({ sites: [{ site_id: "z", latest_run: { run_id: "r0003", status: "failed", result: { error: "docker exit 1" } } }] }).sites;
    expect(failed!.latest_run!.result).toBe("docker exit 1");
  });

  // Same rule as the runner for a resume run: !approved_large && (expected unknown || >= 10000).
  it("matches the runner's large-approval rule", () => {
    const rows = normalizeSitesReport({ sites: [
      { site_id: "big", plan: { ready: true, expected_items: 648013, approved_large: false } },
      { site_id: "boundary", plan: { ready: true, expected_items: 10000 } },
      { site_id: "unknown-size", plan: { ready: true, expected_items: null } },
      { site_id: "big-approved", plan: { ready: true, expected_items: 648013, approved_large: true } },
      { site_id: "unknown-approved", plan: { ready: true, expected_items: null, approved_large: true } },
      { site_id: "below", plan: { ready: true, expected_items: 9999 } },
      { site_id: "no-plan" },
    ] }).sites;
    expect(rows.filter(needsApproval).map((row) => row.site_id)).toEqual(["big", "boundary", "unknown-size"]);
  });

  it("labels cancel rejections", () => {
    expect(describeRejection(409, { reason: "not_active", detail: "done" }).message).toContain("취소할 수 없습니다");
    expect(describeRejection(404, { reason: "not_found" }).message).toContain("없는 작업");
  });

  it("reads runner health from runner_ok/paused", () => {
    expect(normalizeRunnerHealth({ runner_ok: true, paused: true })).toEqual({ ok: true, paused: true, detail: "" });
    expect(normalizeRunnerHealth({ runner_ok: false, reason: "runner_unavailable", detail: null })).toMatchObject({ ok: false, detail: "runner_unavailable" });
  });
});

describe("filterSites", () => {
  const reasons = new Map(normalizeUncollected({ rows: [{ site_id: "sonar-ch-global", class: "dev", reason: "목록 구조 변경", note: "재개발 대기" }] }).map((row) => [row.site_id, row]));

  it("filters by class, including the unclassified bucket", () => {
    expect(ids(filterSites(sites, { cls: "run", lane: "", q: "" }))).toEqual(["ntrs-nasa-gov-search", "e-stat-go-jp-stat-search"]);
    expect(ids(filterSites(sites, { cls: UNCLASSIFIED, lane: "", q: "" }))).toEqual(["fresh-site"]);
    expect(filterSites(sites, { cls: "", lane: "", q: "" })).toHaveLength(5);
  });

  it("filters by lane and combines with class", () => {
    expect(ids(filterSites(sites, { cls: "", lane: "lane-b", q: "" }))).toEqual(["sonar-ch-global", "blocked-example"]);
    expect(ids(filterSites(sites, { cls: "nego", lane: "lane-b", q: "" }))).toEqual(["blocked-example"]);
  });

  it("searches id, detail, reason codes and the uncollected reason", () => {
    expect(ids(filterSites(sites, { cls: "", lane: "", q: "NTRS" }))).toEqual(["ntrs-nasa-gov-search"]);
    expect(ids(filterSites(sites, { cls: "", lane: "", q: "robots" }))).toEqual(["blocked-example"]);
    expect(ids(filterSites(sites, { cls: "", lane: "", q: "api:stat" }))).toEqual(["e-stat-go-jp-stat-search"]);
    expect(ids(filterSites(sites, { cls: "", lane: "", q: "재개발" }, reasons))).toEqual(["sonar-ch-global"]);
  });

  it("finds sites waiting on large approval", () => {
    expect(ids(sites.filter(needsApproval))).toEqual(["e-stat-go-jp-stat-search"]);
  });
});

describe("409 rejection reasons", () => {
  const reasons = ["plan_not_registered", "plan_not_ready", "bad_limit", "large_needs_approval", "unit_reserved", "site_busy",
    "unit_busy", "folder_busy", "external_unknown", "docker_unavailable", "runner_paused"];

  it.each(reasons)("renders %s in Korean instead of the raw code", (reason) => {
    expect(REJECTION_LABELS[reason]).toBeTruthy();
    const { message } = describeRejection(409, { reason });
    expect(message).toBe(REJECTION_LABELS[reason]);
    expect(message).not.toContain(reason);
  });

  it("appends the backend detail and handles a FastAPI-wrapped detail object", () => {
    expect(describeRejection(409, { reason: "site_busy", detail: "run r9 running" }).message).toContain("run r9 running");
    expect(describeRejection(409, { detail: { reason: "unit_busy", detail: "unit u1" } })).toMatchObject({ reason: "unit_busy" });
    expect(describeRejection(409, { detail: "folder_busy" }).reason).toBe("folder_busy");
  });

  it("never drops an unknown reason", () => {
    const { message } = describeRejection(409, { reason: "new_reason", detail: "x" });
    expect(message).toContain("new_reason");
    expect(message).toContain("409");
  });

});

describe("confirm before action", () => {
  it("sends nothing when the operator cancels the confirm dialog", async () => {
    const fetcher = vi.fn();
    const confirm = vi.fn().mockReturnValue(false);
    const outcome = await performAction({ kind: "resume", siteId: "sonar-ch-global" }, { confirm, fetch: fetcher });
    expect(outcome).toEqual({ kind: "skipped" });
    expect(confirm).toHaveBeenCalledOnce();
    expect(confirm.mock.calls[0]![0]).toContain("sonar-ch-global");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("posts a trial with its limit after confirmation", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "r42" }), { status: 201 }));
    const confirm = vi.fn().mockReturnValue(true);
    const outcome = await performAction({ kind: "trial", siteId: "sonar-ch-global", limit: 50 }, { confirm, fetch: fetcher });
    expect(confirm.mock.calls[0]![0]).toContain("시험 수집 50건");
    expect(fetcher).toHaveBeenCalledWith("/api/collect/runs", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(fetcher.mock.calls[0]![1].body)).toEqual({ siteId: "sonar-ch-global", mode: "trial", limit: 50 });
    expect(outcome).toMatchObject({ kind: "ok", runId: "r42" });
  });

  it("confirms cancel with the run id and reports a 409 as a rejection", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ reason: "large_needs_approval" }), { status: 409 }));
    const confirm = vi.fn().mockReturnValue(true);
    const cancel = await performAction({ kind: "cancel", siteId: "ntrs-nasa-gov-search", runId: "r0001" }, { confirm, fetch: fetcher });
    expect(confirm.mock.calls[0]![0]).toContain("r0001");
    expect(fetcher.mock.calls[0]![0]).toBe("/api/collect/runs/r0001/cancel");
    expect(cancel).toMatchObject({ kind: "rejected", reason: "large_needs_approval" });
  });
});

// /site-collect 는 수집 콘솔(collect-console)을 쓴다: BE 응답 원본을 buildConsole 로 묶어 넘긴다.
const NOW = Date.parse("2026-10-20T02:00:00Z") / 1000;
const consoleData = (runnerOk: boolean) => buildConsole({
  sites: { status_source: { ok: true }, runner_ok: runnerOk, runner_reason: runnerOk ? null : "runner_unavailable", documents_ok: true, sites: [
    { site_id: "ntrs-nasa-gov-search", state: "ready", class: "run", detail: "plan 통과", reason_codes: [], status_file: "L2a.csv", documents: 1200,
      plan: { ready: true, why: null, strategy: "api", folder: "f", expected_items: 648013, approved_large: true }, latest_run: null },
    { site_id: "e-stat-go-jp-stat-search", state: "ready", class: "run", detail: "", reason_codes: [], status_file: "L2b.csv", documents: 5,
      plan: { ready: true, why: null, strategy: "api", folder: null, expected_items: 1721215, approved_large: false }, latest_run: null },
  ] },
  uncollected: { sites: [] },
  runs: [{ run_id: "r0001", site_id: "ntrs-nasa-gov-search", mode: "resume", status: "running", started_at: "2026-10-20T01:00:00Z", result: null }],
  health: runnerOk ? { runner_ok: true, paused: false, max_parallel: 2, running: 1, queued: 0 } : { runner_ok: false, reason: "runner_unavailable", detail: "runner is not reachable" },
  stats: null, progress: { sites: [] },
}, null, new Map(), NOW);
const render = (data: ReturnType<typeof buildConsole>) => renderToStaticMarkup(createElement(SiteCollectConsole, { data, fetchedAt: NOW }));

describe("site collection console", () => {
  it("links the selected site to its documents and crawler pages", () => {
    const html = render(consoleData(true));
    // 주의 필요 사이트가 없으면 첫 사이트(ID 순)가 상세에 열린다.
    expect(html).toContain('href="/documents?site_id=e-stat-go-jp-stat-search"');
    expect(html).toContain('href="/crawlers/e-stat-go-jp-stat-search"');
    expect(html).toMatch(/<b>1<\/b><span>승인 대기<\/span>/);
    expect(html).toContain("구역 L2a");
  });

  it("offers cancel, not another start, on a running run", () => {
    const data = consoleData(true);
    const html = render({ ...data, sites: data.sites.filter((site) => site.id === "ntrs-nasa-gov-search") });
    expect(html).toContain(">취소</button>");
    expect(html).toContain("실행기 작업 r0001 이(가) 실행 중입니다");
    expect(html).toMatch(/<button type="button" class="primary" disabled="">/);
  });

  it("disables runner buttons with the reason when the runner is down", () => {
    const html = render(consoleData(false));
    expect(html).toContain("실행기 없음 — 실행기에 연결할 수 없습니다. (runner is not reachable)");
    expect(html).toMatch(/<button type="button" class="primary" disabled="">/);
  });

  it("explains an unavailable backend instead of rendering an empty table", () => {
    const html = render(buildConsole({ sites: null, uncollected: null, runs: null, health: null, stats: null, progress: null }, null, new Map(), NOW));
    expect(html).toContain("사이트 수집 현황을 불러올 수 없습니다");
  });
});

describe("POST /api/collect/runs", () => {
  it("passes a 409 body through untouched so the reason reaches the screen", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ reason: "site_busy", detail: "r7" }), { status: 409 }));
    vi.stubGlobal("fetch", fetcher);
    const response = await postRun(runRequest({ siteId: "sonar-ch-global", mode: "resume" }));
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ reason: "site_busy", detail: "r7" });
    expect(JSON.parse(fetcher.mock.calls[0]![1].body)).toEqual({ site_id: "sonar-ch-global", mode: "resume" });
  });

  it("rejects a bad limit before calling the backend", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const response = await postRun(runRequest({ siteId: "a-site", mode: "trial", limit: 0 }));
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ reason: "bad_limit" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    ["cross-site text/plain", { "content-type": "text/plain", origin: "https://evil.example", "sec-fetch-site": "cross-site" }],
    ["same-origin text/plain", { "content-type": "text/plain", origin: "http://localhost", "sec-fetch-site": "same-origin" }],
    ["cross-site JSON", { "content-type": "application/json", origin: "https://evil.example", "sec-fetch-site": "cross-site" }],
    ["same-site sibling", { "content-type": "application/json", origin: "http://localhost", "sec-fetch-site": "same-site" }],
    ["no Sec-Fetch-Site, foreign Origin", { "content-type": "application/json", origin: "https://evil.example" }],
    ["no Sec-Fetch-Site, no Origin", { "content-type": "application/json" }],
  ])("rejects a %s POST without calling the backend", async (_label, headers) => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const response = await postRun(runRequest({ siteId: "a-site", mode: "trial", limit: 5 }, headers));
    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({ reason: "cross_origin" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("accepts a same-origin JSON POST from a browser without Sec-Fetch-Site", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "r0009" }), { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    const response = await postRun(runRequest({ siteId: "a-site", mode: "trial", limit: 5 }, { "content-type": "application/json; charset=utf-8", origin: "http://localhost" }));
    expect(response.status).toBe(201);
  });
});

describe("POST /api/collect/runs/{runId}/cancel", () => {
  const cancelRequest = (headers: Record<string, string>) =>
    new Request("http://localhost/api/collect/runs/r0001/cancel", { method: "POST", headers, body: "{}" });
  const context = { params: Promise.resolve({ runId: "r0001" }) };

  it("rejects a cross-site text/plain cancel", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const response = await postCancel(cancelRequest({ "content-type": "text/plain", origin: "https://evil.example", "sec-fetch-site": "cross-site" }), context);
    expect(response.status).toBe(403);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("forwards a same-origin cancel without a requested_by body", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "r0001", status: "cancelling" }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const response = await postCancel(cancelRequest(SAME_ORIGIN), context);
    expect(response.status).toBe(200);
    expect(fetcher.mock.calls[0]![0]).toMatch(/\/collect\/runs\/r0001\/cancel$/);
    expect(fetcher.mock.calls[0]![1].body).toBeUndefined();
  });
});

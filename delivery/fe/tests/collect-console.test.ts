import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  ago, buildConsole, classOf, classText, laneNames, countClasses, countStates, filtered, isBig, namesBy, normalizeProgress, normalizeRunnerRuns,
  normalizeRunnerStatus, normalizeStats, percentOf, scoped, secondaryCounts, sortSites, startCheck, stateOf, tsOf,
  type BackfillRow, type ConsoleSite, type RunnerRun, type Scope,
} from "../src/lib/collect-console";
import { normalizeSite } from "../src/lib/site-collect";

const NOW = Date.parse("2026-10-20T12:00:00Z") / 1000;

// BE /collect/runs 는 실행기 run_row 를 그대로 넘긴다 (deploy/collect-runner/runner.py).
const runRow = (over: Partial<Record<string, unknown>> = {}): Record<string, unknown> => ({
  run_id: "r0001", site_id: "a-site", mode: "resume", limit: null, status: "done", run_folder: "20261019-a",
  started_at: "2026-10-20T10:00:00Z", finished_at: "2026-10-20T11:00:00Z", requested_at: "2026-10-20T09:59:00Z",
  requested_by: "op", result: { site_status: "complete", stop_reason: null, saved: 120, failed: 2 }, reason: null, ...over,
});
const run = (over: Partial<Record<string, unknown>> = {}): RunnerRun => normalizeRunnerRuns([runRow(over)])[0]!;
const bf = (over: Partial<BackfillRow> = {}): BackfillRow => ({
  site_id: "a-site", items_done: 10, total_estimate: 100, updated_at: "2026-10-20T11:55:00Z", completed_at: null,
  active_status: null, active_job_id: null, docs_in_db: 10, has_cursor: true, ...over,
});

// BE /collect/sites 한 줄 (delivery/be/collect.py site_table).
const siteRow = (over: Partial<Record<string, unknown>> = {}): Record<string, unknown> => ({
  site_id: "a-site", state: "ready", class: "run", class_label: "실행 가능", detail: "plan 통과", reason_codes: [],
  updated_at: "2026-10-19T08:00:00Z", status_file: "L2a.csv", documents: 50,
  plan: { ready: true, why: null, strategy: "listing", folder: "20261019-a", expected_items: 500, approved_large: false },
  latest_run: null, ...over,
});

describe("time helpers", () => {
  it("parses postgres, ISO and Z timestamps", () => {
    expect(tsOf("2026-10-20 12:00:00+00")).toBe(NOW);
    expect(tsOf("2026-10-20T12:00:00Z")).toBe(NOW);
    expect(tsOf("nope")).toBeNull();
    expect(tsOf(null)).toBeNull();
  });
  it("formats ago like the console", () => {
    expect(ago(30)).toBe("30초 전");
    expect(ago(600)).toBe("10분 전");
    expect(ago(7200)).toBe("2.0시간 전");
    expect(ago(3 * 86400)).toBe("3일 전");
    expect(ago(null)).toBe("-");
    expect(ago(-5)).toBe("0초 전");
  });
});

describe("normalizers", () => {
  it("keeps the runner run_row fields", () => {
    const r = run({ status: "failed", reason: "exit_1", result: { error: "no db" } });
    expect(r).toMatchObject({ run_id: "r0001", status: "failed", reason: "exit_1", run_folder: "20261019-a", requested_by: "op" });
    expect(r.result).toEqual({ site_status: null, stop_reason: null, saved: null, failed: null, error: "no db" });
  });
  it("treats an error body from /collect/runs as no runs", () => {
    expect(normalizeRunnerRuns({ reason: "runner_unavailable", detail: "x" })).toEqual([]);
    expect(normalizeRunnerRuns([{ site_id: "x" }])).toEqual([]);
  });
  it("reads /progress rows and whether a cursor exists", () => {
    const rows = normalizeProgress({ sites: [
      { site_id: "a", cursor: { page: 3 }, items_done: 30, total_estimate: 90, updated_at: "2026-10-20T11:00:00Z", completed_at: null, active_status: "running", active_job_id: 7, docs_in_db: 31 },
      { site_id: "b", cursor: null, items_done: 0, total_estimate: null, updated_at: null, completed_at: null, active_status: null, active_job_id: null, docs_in_db: 0 },
    ] });
    expect(rows[0]).toMatchObject({ site_id: "a", has_cursor: true, active_job_id: 7, total_estimate: 90 });
    expect(rows[1]).toMatchObject({ site_id: "b", has_cursor: false, total_estimate: null });
  });
  it("reads /stats overview and runner health", () => {
    expect(normalizeStats({ overview: { sites: 3, documents: 9, latest_collected_at: "x" }, measured_at: "m" }))
      .toMatchObject({ sites: 3, documents: 9, measured_at: "m" });
    expect(normalizeStats(null)).toBeNull();
    expect(normalizeRunnerStatus({ runner_ok: true, reason: null, detail: null, paused: false, max_parallel: 2, running: 1, queued: 0 }))
      .toEqual({ ok: true, reason: null, detail: null, paused: false, max_parallel: 2, running: 1, queued: 0 });
    expect(normalizeRunnerStatus({ runner_ok: false, reason: "runner_unavailable", detail: "runner is not reachable" }))
      .toMatchObject({ ok: false, reason: "runner_unavailable" });
  });
});

describe("stateOf", () => {
  it("maps runner outcomes onto console states", () => {
    expect(stateOf(run({ status: "running", result: null }), null, NOW).state).toBe("run");
    expect(stateOf(run({ status: "queued", result: null }), null, NOW)).toMatchObject({ state: "run", why: "대기 중" });
    expect(stateOf(run({ status: "failed", reason: "exit_2", result: null }), null, NOW)).toMatchObject({ state: "stop", alert: "멈춤: 실행기 작업 r0001 실패 · exit_2" });
    expect(stateOf(run({ status: "cancelled", result: null }), null, NOW)).toMatchObject({ state: "review", why: "취소됨" });
    expect(stateOf(run(), null, NOW).state).toBe("done");
    expect(stateOf(run({ result: { site_status: "access_blocked", stop_reason: "robots_disallowed" } }), null, NOW))
      .toMatchObject({ state: "lock", why: "robots.txt 금지" });
    expect(stateOf(run({ result: { site_status: "complete_with_mismatch" } }), null, NOW)).toMatchObject({ state: "review", why: "완료(총건수 불일치)" });
    expect(stateOf(run({ result: { site_status: "complete_with_review" } }), null, NOW)).toMatchObject({ state: "review", why: "" });
  });
  it("flags a running backfill that stopped updating for 30 minutes", () => {
    const stale = bf({ active_status: "running", active_job_id: 9, updated_at: "2026-10-20T11:00:00Z" });
    expect(stateOf(null, stale, NOW)).toMatchObject({ state: "stop", alert: "멈춤 의심 · 마지막 갱신 60분 전 (백필 작업 #9)" });
    expect(stateOf(null, bf({ active_status: "running" }), NOW).state).toBe("run");
  });
  it("uses backfill completion when the runner has no record", () => {
    expect(stateOf(null, bf({ completed_at: "2026-10-19T00:00:00Z" }), NOW).state).toBe("done");
    expect(stateOf(null, bf(), NOW)).toMatchObject({ state: "review", why: "백필 중단됨" });
    expect(stateOf(null, bf({ has_cursor: false }), NOW).state).toBe("idle");
    expect(stateOf(null, null, NOW).state).toBe("idle");
  });
});

describe("classOf", () => {
  const site = normalizeSite(siteRow({ class: "dev", state: "needs_dev", detail: "ext:pdf 필요" }));
  it("prefers run evidence over the site-status judgment (console priority)", () => {
    expect(classOf(site, run(), "done", "")).toMatchObject({ k: "run", src: "실행 기록", why: "실행 기록 있음 · 완료" });
    expect(classOf(site, run({ status: "failed", result: null }), "stop", "")).toMatchObject({ k: "dev", why: "실행기 실행 실패 (r0001)" });
    expect(classOf(site, run({ result: { site_status: "config_error" } }), "review", "")).toMatchObject({ k: "dev", why: "설정 오류로 멈춘 실행" });
    expect(classOf(site, run(), "lock", "robots.txt 금지")).toMatchObject({ k: "nego", why: "마지막 실행에서 접근 차단 · robots.txt 금지" });
  });
  it("falls back to the judgment file, then to 판정 없음", () => {
    expect(classOf(site, null, "idle", "")).toEqual({ k: "dev", why: "수집기 개발 필요 · ext:pdf 필요", src: "site-status/L2a.csv (2026-10-19T08:00:00Z)", lane: "L2a" });
    expect(classOf(null, null, "idle", "")).toMatchObject({ k: "none", why: "판정 기록 없음" });
  });
});

const fullInput = () => ({
  sites: { status_source: { ok: true, reason: null, files: 2 }, runner_ok: true, runner_reason: null, documents_ok: true, sites: [
    siteRow(),
    siteRow({ site_id: "big-site", plan: { ready: true, why: null, strategy: "api", folder: null, expected_items: null, approved_large: false }, documents: 150_000 }),
    siteRow({ site_id: "blocked-site", class: "nego", state: "blocked_access", detail: "403", plan: null, documents: 0 }),
  ] },
  uncollected: { count: 2, sites: [
    { site_id: "blocked-site", class: "nego", uncollected_reasons: ["class_nego", "no_documents"], note: null },
    { site_id: "note-only", class: "", uncollected_reasons: ["class_none"], note: "사용자 결정" },
  ] },
  runs: [runRow({ run_id: "r0003", site_id: "a-site", status: "running", result: null, started_at: "2026-10-20T11:50:00Z" }), runRow({ run_id: "r0002", site_id: "a-site" })],
  health: { runner_ok: true, reason: null, detail: null, paused: false, max_parallel: 2, running: 1, queued: 0 },
  stats: { overview: { sites: 806, documents: 1234 }, measured_at: "2026-10-20T12:00:00Z" },
  progress: { sites: [{ site_id: "big-site", cursor: { p: 1 }, items_done: 140_000, total_estimate: 300_000, updated_at: "2026-10-20T11:58:00Z", completed_at: null, active_status: "running", active_job_id: 4, docs_in_db: 150_000 }] },
});
const TAX = { version: "2026-08-12", sites: { "a-site": { country: "프랑스", doc_type: "보고서" }, "big-site": { country: "독일", doc_type: "통계" } } };

describe("buildConsole", () => {
  const data = buildConsole(fullInput(), TAX, new Map([["a-site", "A 사이트"]]), NOW);
  const by = (id: string): ConsoleSite => data.sites.find((s) => s.id === id)!;

  it("joins every source into one row per site", () => {
    expect(data.sites.map((s) => s.id)).toEqual(["a-site", "big-site", "blocked-site", "note-only"]);
    expect(data).toMatchObject({ runnerOk: true, sitesOk: true, statusOk: true, documentsOk: true, runsOk: true, progressOk: true, uncollectedOk: true, taxonomyVersion: "2026-08-12" });
    expect(data.stats?.documents).toBe(1234);
  });
  it("uses the live run as the current state and keeps run history newest first", () => {
    const a = by("a-site");
    expect(a).toMatchObject({ name: "A 사이트", country: "프랑스", type: "보고서", state: "run", total: 500, totalSrc: "plan 예상 건수" });
    expect(a.runs.map((r) => r.run_id)).toEqual(["r0003", "r0002"]);
    // 실행 중인 r0003 은 결과가 없어, 결과가 있는 r0002 의 저장 120(DB 50 보다 큼)과 실패 2 를 쓴다.
    expect(a).toMatchObject({ saved: 120, savedSrc: "실행기 결과 저장 건수", failed: 2 });
  });
  it("takes the backfill estimate as the site total and the largest saved count", () => {
    const b = by("big-site");
    expect(b).toMatchObject({ state: "run", saved: 150_000, savedSrc: "DB 문서 수", total: 300_000, approval: true });
    expect(isBig(b)).toBe(true);
    expect(percentOf(b)).toBe(50);
  });
  it("marks uncollected reasons and notes, and unknown taxonomy as 미분류", () => {
    expect(by("blocked-site").uncollected).toEqual({ reasons: ["협의 필요", "수집 문서 0건"], note: "" });
    expect(by("note-only")).toMatchObject({ country: "미분류", type: "미분류", uncollected: { reasons: ["판정 없음"], note: "사용자 결정" } });
  });
  it("survives a backend that answered nothing", () => {
    const empty = buildConsole({ sites: null, uncollected: null, runs: null, health: null, stats: null, progress: null }, null, new Map(), NOW);
    expect(empty).toMatchObject({ sites: [], runnerOk: false, sitesOk: false, runsOk: false, stats: null, runnerText: "연결 안 됨" });
  });
  it("falls back to latest_run when /collect/runs failed and reports why the runner is down", () => {
    const input = { ...fullInput(), runs: { reason: "runner_unavailable" }, health: { runner_ok: false, reason: "runner_unavailable", detail: "runner is not reachable" } };
    input.sites.sites[0] = siteRow({ latest_run: runRow({ run_id: "r0009", status: "failed", result: null }) });
    const d = buildConsole(input, TAX, new Map(), NOW);
    expect(d.runsOk).toBe(false);
    expect(d.sites.find((s) => s.id === "a-site")).toMatchObject({ state: "stop" });
    expect(d.runnerText).toBe("실행기에 연결할 수 없습니다. (runner is not reachable)");
  });
});

describe("DB-only sites and direct runs", () => {
  const empty = { uncollected: null, runs: [], health: null, stats: null, progress: null };
  const direct = { status: "direct_running", running: true, saved: 354455, run_id: "direct", run_folder: "20261019-s155",
    started_at: "2026-10-20T10:00:00Z", updated_at: "2026-10-20T11:50:00Z", detail: "incomplete/time_budget, 문서 354455" };
  it("lists a site that only has DB documents with its count and the BE label", () => {
    const data = buildConsole({ ...empty, sites: { runner_ok: false, documents_ok: true, progress_source: { ok: true }, sites: [
      siteRow({ site_id: "scienceon-api", state: null, class: "", class_label: "API 수집", source: "db_only", status_file: null,
        documents: 3_400_000, plan: null }),
    ] } }, null, new Map(), NOW);
    const sci = data.sites[0]!;
    expect(sci).toMatchObject({ id: "scienceon-api", state: "idle", saved: 3_400_000, savedSrc: "DB 문서 수" });
    expect(sci.cls).toMatchObject({ k: "none", label: "API 수집", src: "DB 문서 수" });
    expect(classText(sci.cls)).toBe("API 수집");
    expect(data.directOk).toBe(true);
  });
  it("shows a direct run as 수집 중 with saved count and refresh time even without runner records", () => {
    const data = buildConsole({ ...empty, runs: null, sites: { runner_ok: false, documents_ok: true, sites: [
      siteRow({ site_id: "prism", documents: 2900, plan: null, direct }),
      siteRow({ site_id: "done-site", direct: { ...direct, status: "done", running: false, saved: 10 } }),
    ] } }, null, new Map(), NOW);
    const prism = data.sites.find((s) => s.id === "prism")!;
    expect(prism.state).toBe("run");
    expect(prism.why).toBe("수집 중(직접 실행) · 저장 354,455건 · 갱신 10분 전");
    expect(prism.saved).toBe(354455);
    expect(prism.savedSrc).toContain("직접 실행 저장 건수");
    expect(prism.lastTs).toBe(tsOf("2026-10-20T11:50:00Z"));
    expect(data.sites.find((s) => s.id === "done-site")!.state).toBe("idle");
    expect(data.directOk).toBeNull();  // 예전 BE: progress_source 없음
  });
  it("keeps a live runner job ahead of the direct-run text", () => {
    expect(stateOf(run({ status: "running" }), null, NOW, normalizeSite(siteRow({ direct }))!.direct).why).toBe("");
  });
});

describe("filters and grouping", () => {
  const data = buildConsole(fullInput(), TAX, new Map(), NOW);
  const none: Scope = { attn: false, type: "", country: "", cls: "", special: "" };

  it("scopes by attention, axis, class and the 승인 대기 / 미수집 views", () => {
    expect(scoped(data.sites, { ...none, attn: true }).map((s) => s.id)).toEqual([]);
    expect(scoped(data.sites, { ...none, country: "독일" }).map((s) => s.id)).toEqual(["big-site"]);
    expect(scoped(data.sites, { ...none, cls: "nego" }).map((s) => s.id)).toEqual(["blocked-site"]);
    expect(scoped(data.sites, { ...none, special: "approval" }).map((s) => s.id)).toEqual(["big-site"]);
    expect(scoped(data.sites, { ...none, special: "uncollected" }).map((s) => s.id)).toEqual(["blocked-site", "note-only"]);
  });
  it("filters by judgment-file lane (예전 사이트 수집 표의 구역 필터)", () => {
    expect(laneNames(data.sites)).toEqual(["L2a"]);
    expect(scoped(data.sites, { ...none, lane: "L2a" }).map((s) => s.id)).toEqual(["a-site", "big-site", "blocked-site"]);
    expect(scoped(data.sites, { ...none, lane: "L9" })).toEqual([]);
  });
  it("filters by state and search text", () => {
    expect(filtered(data.sites, { ...none, state: "run", q: "" }).map((s) => s.id)).toEqual(["a-site", "big-site"]);
    expect(filtered(data.sites, { ...none, state: "", q: "BLOCK" }).map((s) => s.id)).toEqual(["blocked-site"]);
  });
  it("sorts by state order, then size, then id", () => {
    expect(sortSites(data.sites).map((s) => s.id)).toEqual(["big-site", "a-site", "blocked-site", "note-only"]);
  });
  it("counts states, classes and axis names with 미분류 last", () => {
    expect(countStates(data.sites)).toEqual({ run: 2, stop: 0, review: 0, done: 0, lock: 0, idle: 2 });
    expect(countClasses(data.sites)).toEqual({ run: 2, dev: 0, nego: 1, skip: 0, none: 1 });
    expect(namesBy(data.sites, "country").at(-1)).toBe("미분류");
    expect(secondaryCounts(data.sites, "country")[0]).toEqual(["미분류", 2]);
  });
});

describe("startCheck", () => {
  const data = buildConsole(fullInput(), TAX, new Map(), NOW);
  const by = (id: string): ConsoleSite => data.sites.find((s) => s.id === id)!;
  const health = data.runner;

  it("blocks both buttons while the runner is down and says why", () => {
    const check = startCheck(by("note-only"), null, false, "실행기에 연결할 수 없습니다.");
    expect(check.blocked[0]).toBe("실행기 없음 — 실행기에 연결할 수 없습니다.");
  });
  it("blocks a site with a live run, no plan, or nego class", () => {
    expect(startCheck(by("a-site"), health, true, "").blocked).toEqual(["실행기 작업 r0003 이(가) 실행 중입니다"]);
    expect(startCheck(by("blocked-site"), health, true, "").blocked).toEqual([
      "실행기에 등록된 plan 이 없습니다 (협의 필요 · 접근 제한 · 403)",
      "접근 제한·협의 필요 사이트는 시작할 수 없습니다",
    ]);
  });
  it("holds only resume for a site awaiting large-run approval", () => {
    const check = startCheck({ ...by("big-site"), state: "idle", runs: [] }, health, true, "");
    expect(check.blocked).toEqual([]);
    expect(check.resumeBlocked[0]).toMatch(/^승인 대기 — 예상 미상/);
  });
  it("reports a paused runner", () => {
    const check = startCheck(by("note-only"), { ...health!, paused: true }, true, "");
    expect(check.blocked[0]).toBe("실행기가 일시 중지돼 있어 새 작업을 받지 않습니다");
  });
});

describe("vendored taxonomy", () => {
  // 운영 FE 이미지에는 delivery/data 가 없어서 분류를 번들에 넣는다. 원본과 같아야 한다.
  it("matches delivery/data/site-taxonomy.json", () => {
    const vendored = readFileSync(new URL("../src/vendor/site-taxonomy.json", import.meta.url), "utf8");
    const source = readFileSync(new URL("../../data/site-taxonomy.json", import.meta.url), "utf8");
    expect(JSON.parse(vendored)).toEqual(JSON.parse(source));
  });
});

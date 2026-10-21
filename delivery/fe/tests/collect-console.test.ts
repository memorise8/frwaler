import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  accessBlock, ago, buildConsole, classOf, classText, reasonText, runExplain, laneNames, countClasses, countStates, filtered, isBig, namesBy, normalizeProgress, normalizeRunnerRuns,
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
    expect(stateOf(run({ result: { site_status: "complete_with_mismatch" } }), null, NOW)).toMatchObject({ state: "done", why: "완료(수치 확인)" });
    expect(stateOf(run({ result: { site_status: "complete_with_review" } }), null, NOW)).toMatchObject({ state: "review", why: "" });
  });
  it("does not call a finished run with rc 2 a stop: run-pilot exits 2 for results that need review", () => {
    const failed = (site_status: string | null, extra: Record<string, unknown> = {}) =>
      stateOf(run({ status: "failed", reason: "exit_2", result: { site_status, stop_reason: null, saved: 5, failed: 0, ...extra } }), null, NOW);
    expect(failed("complete_with_mismatch")).toMatchObject({ state: "done", alert: null, why: "완료(수치 확인)" });
    expect(failed("complete_with_review")).toMatchObject({ state: "review", alert: null });
    expect(failed("complete_unverified")).toMatchObject({ state: "review", why: "완료(검증 안 됨)" });
    expect(failed("access_blocked")).toMatchObject({ state: "lock", alert: null });
    expect(failed("config_error")).toMatchObject({ state: "stop", alert: "멈춤: 실행기 작업 r0001 실패 · exit_2 · 설정 오류" });
    expect(failed("running")).toMatchObject({ state: "stop", alert: "멈춤: 실행기 작업 r0001 실패 · exit_2 · 실행 중 중단" });
    expect(failed("complete", { error: "result.json 없음" }).state).toBe("stop");
  });
  it("lets a later direct run in the O1 status file replace an old runner failure", () => {
    const bad = run({ status: "failed", reason: "exit_1", started_at: "2026-10-19T21:00:00Z", result: { site_status: "config_error" } });
    const o1 = (over: Record<string, unknown>) => normalizeSite(siteRow({ direct: {
      status: "done", running: false, saved: 50789, run_id: "direct", started_at: "2026-10-20T04:57:38Z", finished_at: "2026-10-20T07:26:44Z",
      updated_at: "2026-10-20T11:00:00Z", detail: "직접 실행: complete/last_page", ...over } }))!.direct;
    expect(stateOf(bad, null, NOW, o1({}))).toMatchObject({ state: "done", alert: null, why: "직접 실행으로 완료" });
    expect(stateOf(bad, null, NOW, o1({ detail: "complete_with_mismatch, 문서 10" })).why).toBe("완료(수치 확인) · 직접 실행");
    expect(stateOf(bad, null, NOW, o1({ status: "done(접근 거부)" })).state).toBe("lock");
    // 실행기 작업보다 먼저 시작한 직접 실행, 또는 끝나지 않은 직접 실행은 실패를 덮지 않는다.
    expect(stateOf(bad, null, NOW, o1({ started_at: "2026-10-18T00:00:00Z", finished_at: "2026-10-18T01:00:00Z" })).state).toBe("stop");
    expect(stateOf(bad, null, NOW, o1({ status: "failed" })).state).toBe("stop");
    // 같은 작업(run_id 같음)의 O1 "done" 은 처리 끝이라는 뜻일 뿐: 실행기 결과를 그대로 쓴다.
    expect(stateOf(bad, null, NOW, o1({ run_id: "r0001", started_at: null, finished_at: null })).state).toBe("stop");
    // 다만 O1 이 같은 작업을 접근 거부로 정리했으면 접근 제한.
    expect(stateOf(bad, null, NOW, o1({ run_id: "r0001", status: "done(접근 거부)" }))).toMatchObject({ state: "lock", via: "direct" });
    // 뒤의 직접 실행이 미완료·검토로 끝났으면 완료가 아니다.
    expect(stateOf(bad, null, NOW, o1({ detail: "incomplete/listing_failed:http_4xx, 문서 0" }))).toMatchObject({ state: "review", why: "미완료 · 직접 실행" });
    expect(stateOf(bad, null, NOW, o1({ detail: "complete_with_review/adapter_contract_failures:3" })).state).toBe("review");
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
    expect(classOf(site, run({ status: "failed", result: { site_status: "complete_with_review" } }), "review", "")).toMatchObject({ k: "run" });
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
      siteRow({ site_id: "done-site", direct: { ...direct, status: "done", running: false, saved: 10, detail: "complete/last_page, 문서 10" } }),
    ] } }, null, new Map(), NOW);
    const prism = data.sites.find((s) => s.id === "prism")!;
    expect(prism.state).toBe("run");
    expect(prism.why).toBe("수집 중(직접 실행) · 저장 354,455건 · 갱신 10분 전");
    expect(prism.saved).toBe(354455);
    expect(prism.savedSrc).toContain("직접 실행 저장 건수");
    expect(prism.lastTs).toBe(tsOf("2026-10-20T11:50:00Z"));
    expect(data.sites.find((s) => s.id === "done-site")!).toMatchObject({ state: "done", why: "직접 실행으로 완료" });
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

describe("state, class and access agree (운영 808곳 모순 점검에서 나온 경우)", () => {
  const input = (sites: Record<string, unknown>[], runs: Record<string, unknown>[]) =>
    ({ sites: { runner_ok: true, documents_ok: true, progress_source: { ok: true }, sites }, uncollected: null, runs, health: null, stats: null, progress: null });
  const doneDirect = { status: "done", running: false, saved: 389075, run_id: "direct", started_at: "2026-10-20T22:04:49Z",
    finished_at: "2026-10-21T00:10:13Z", updated_at: "2026-10-21T00:15:00Z", detail: "complete/last_page_empty_reached_start, 문서 389075" };
  it("re-classes a site finished by a later direct run, keeping the runner failure only as a note (prism)", () => {
    const data = buildConsole(input([siteRow({ site_id: "prism", direct: doneDirect })],
      [runRow({ run_id: "r0590", site_id: "prism", status: "failed", reason: "exit_1", started_at: "2026-10-19T06:32:20Z",
        result: { site_status: "config_error", stop_reason: "adapter_contract_failures_consecutive:empty_item" } })]), null, new Map(), NOW);
    const s = data.sites[0]!;
    expect(s).toMatchObject({ state: "done", via: "direct", why: "직접 실행으로 완료" });
    expect(s.cls).toMatchObject({ k: "run", why: "직접 실행 결과 · 직접 실행으로 완료", src: "O1 상태 (직접 실행)" });
    expect(accessBlock(s)).toBeNull();
  });
  it("calls a site with only earlier DB documents 새 수집 전, with the count", () => {
    const data = buildConsole(input([siteRow({ site_id: "old", class: "dev", state: "needs_dev", detail: "ext:pdf", documents: 318, plan: null })], []),
      null, new Map(), NOW);
    const s = data.sites[0]!;
    expect(s).toMatchObject({ state: "idle", via: "db", why: "이전 수집분 DB 318건" });
    expect(s.cls.why).toBe("이전 수집분 DB 에 있음 · 새 수집 판정: 수집기 개발 필요 · ext:pdf");
    expect(buildConsole(input([siteRow({ site_id: "empty", documents: 0, plan: null })], []), null, new Map(), NOW).sites[0]!)
      .toMatchObject({ state: "idle", why: "", via: null });
  });
  it("does not let a judgment-file access block outlive a later successful direct run", () => {
    const data = buildConsole(input([siteRow({ site_id: "ok", class: "nego", state: "blocked_access", direct: doneDirect })], []), null, new Map(), NOW);
    expect(accessBlock(data.sites[0]!)).toBeNull();
  });
});

describe("plain-language reasons", () => {
  it("explains runner and collector codes, keeping counts and unknown codes", () => {
    expect(reasonText("robots_403")).toBe("robots.txt 요청이 403 으로 막힘");
    expect(reasonText("robots_500")).toBe("robots.txt 확인에 실패함");
    expect(reasonText("skipped_by_rule:468")).toBe("규칙으로 건너뛴 항목 468건");
    expect(reasonText("adapter_contract_failures:1755")).toBe("수집 규칙(필수 칸)을 못 채운 항목 1,755건");
    expect(reasonText("exit_2")).toBe("결과 확인이 필요한 상태로 끝남(rc 2)");
    expect(reasonText("exit_7")).toBe("수집기가 7 코드로 끝남");
    expect(reasonText("weird_code")).toBe("weird_code");
    expect(reasonText("adapter_contract_failures_consecutive:empty_item")).toBe("수집 규칙을 못 채운 항목이 연달아 나와 멈춤 · 내용이 빈 항목");
    expect(reasonText(null)).toBe("");
    expect(reasonText("access_refused")).toBe("사이트가 접근을 거부함");
    expect(reasonText("robots_unavailable:http_500")).toBe("robots.txt 를 받지 못함 · http_500");
  });
  it("summarises the last runner job for the detail panel", () => {
    expect(runExplain(run({ status: "failed", reason: "exit_2", result: { site_status: "complete_with_mismatch", stop_reason: null } })))
      .toBe("작업 r0001 · 완료(총건수 불일치) · 결과 확인이 필요한 상태로 끝남(rc 2)");
    expect(runExplain(run({ status: "failed", reason: "exit_2", result: { site_status: "access_blocked", stop_reason: "robots_403" } })))
      .toBe("작업 r0001 · 접근 차단 · robots.txt 요청이 403 으로 막힘 (robots_403) · 결과 확인이 필요한 상태로 끝남(rc 2)");
    expect(runExplain(null)).toBe("");
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
      "접근 제한·협의 필요 사이트는 시작할 수 없습니다 (판정 파일에 접근 제한으로 기록됨) — 우회·재시도하지 않습니다",
    ]);
  });
  it("blocks trial and resume when the last run was access-blocked, even if the plan is ready (homeaffairs)", () => {
    const input = fullInput();
    input.sites.sites.push(siteRow({ site_id: "homeaffairs", class: "nego", state: "blocked_access", detail: "robots_403", documents: 0 }));
    input.runs.push(runRow({ run_id: "r0376", site_id: "homeaffairs", mode: "trial", status: "failed", reason: "exit_2",
      result: { site_status: "access_blocked", stop_reason: "robots_403", saved: 0, failed: 0 } }));
    const site = buildConsole(input, TAX, new Map(), NOW).sites.find((x) => x.id === "homeaffairs")!;
    expect(site.state).toBe("lock");
    expect(accessBlock(site)).toBe("마지막 실행(r0376)에서 접근 차단 · robots.txt 요청이 403 으로 막힘");
    const check = startCheck(site, health, true, "");
    expect(check.blocked).toHaveLength(1);
    expect(check.blocked[0]).toMatch(/^접근 제한·협의 필요 사이트는 시작할 수 없습니다 \(마지막 실행\(r0376\)/);
    // 콘솔 상태가 lock 이 아니어도(예: 판정이 바뀌기 전 데이터) 실행 결과만으로 막는다.
    expect(accessBlock({ ...site, state: "stop", status: null, cls: { ...site.cls, k: "run" } })).toMatch(/^마지막 실행\(r0376\)/);
    expect(accessBlock(by("a-site"))).toBeNull();
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

describe("state chips (site-collect-console.tsx)", () => {
  const source = readFileSync(new URL("../src/app/(shell)/site-collect/site-collect-console.tsx", import.meta.url), "utf8");
  const chips = source.slice(source.indexOf('aria-label="상태 요약"'), source.indexOf('aria-label="실행 구분"'));
  it("are buttons that toggle the same stateFilter as the 상태 필터 dropdown", () => {
    expect(chips).toContain("STATE_KEYS.map((k) => <button");
    expect(chips).toContain("aria-pressed={stateFilter === k}");
    expect(chips).toContain("onClick={() => toggleState(k)}");
    expect(chips).toContain("disabled={!counts[k] && stateFilter !== k}");
    expect(source).toContain('setStateFilter(stateFilter === value ? "" : value)');
    expect(source).toMatch(/<select value=\{stateFilter\} aria-label="상태 필터"/);
  });
  it("no longer lists every stopped site at the top", () => {
    expect(source).not.toContain("stopped.map(");
    expect(source).toContain('className="alert summary"');
  });
});

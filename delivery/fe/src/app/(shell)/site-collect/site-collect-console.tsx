"use client";

// 사이트 수집(/site-collect): plan-dev-20261008/dashboard/index.html 수집 콘솔의
// render()/renderDetail()/startBox() 를 React 로 옮긴 것. 판단(상태·구분·필터·시작 전 점검)은 모두 @/lib/collect-console 에 있다.

import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import "./collect-console.css";
import {
  accessBlock, ago, CLASS_KEYS, CLASSES, classText, countClasses, countStates, filtered, fmt, isBig, MIX_ORDER, namesBy, needsAttention, runExplain,
  laneNames, pctText, percentOf, RESULT_STATUS_KO, RUNNER_STATUS_KO, scoped, secondaryCounts, sortSites, startCheck, STATE_KEYS,
  STATES, tsOf, type ConsoleData, type ConsoleSite, type ConsoleState, type Scope,
} from "@/lib/collect-console";
import { crawlerDocumentsHref } from "@/lib/crawler-evidence";
import { performAction, runModeLabel, stateLabel, TRIAL_LIMIT_MAX, type CollectAction } from "@/lib/site-collect";

type View = "type" | "country" | "matrix";
type Special = Scope["special"];
type Msg = Readonly<{ ok: boolean; t: string }>;

const PAGE = 40;
const REFRESH_MS = 15_000;
const THEMES = [["light", "밝게"], ["dark", "어둡게"], ["auto", "시스템 설정"]] as const;
const MIXCOL: Readonly<Record<ConsoleState, string>> = {
  run: "var(--run)", stop: "var(--stop)", review: "var(--review)", done: "var(--done)", lock: "var(--lock)", idle: "var(--bar-bg)",
};
const SPECIAL_LABEL: Readonly<Record<Exclude<Special, "">, string>> = { approval: "승인 대기", uncollected: "미수집" };

const Badge = ({ state }: { state: ConsoleState }) => (
  <span className={`badge b-${state}`}>{state === "run" ? <span className="pulse" /> : <i>{STATES[state].mark}</i>}{STATES[state].label}</span>
);
const ClsPill = ({ site }: { site: ConsoleSite }) => <span className={`cls ${CLASSES[site.cls.k].tone}`}>{classText(site.cls)}</span>;
const MixBar = ({ list }: { list: readonly ConsoleSite[] }) => {
  const c = countStates(list);
  return <div className="mix">{MIX_ORDER.map((k) => c[k] ? <i key={k} style={{ width: `${(c[k] / list.length) * 100}%`, background: MIXCOL[k] }} /> : null)}</div>;
};

export default function CollectConsole({ data, fetchedAt }: Readonly<{ data: ConsoleData; fetchedAt: number }>) {
  const router = useRouter();
  // 이 화면에서 이어받기를 요청했다가 대량 승인 필요로 거절된 사이트: BE 에 반영되기 전에도 승인 대기에 보인다.
  const [held, setHeld] = useState<readonly string[]>([]);
  const all = useMemo(() => held.length ? data.sites.map((s) => !s.approval && held.includes(s.id) ? { ...s, approval: true } : s) : data.sites, [data.sites, held]);
  const [view, setView] = useState<View>("type");
  const [onlyAttn, setOnlyAttn] = useState(true);
  const [selType, setSelType] = useState("");
  const [selCountry, setSelCountry] = useState("");
  const [selCls, setSelCls] = useState("");
  const [lane, setLane] = useState("");
  const [special, setSpecial] = useState<Special>("");
  const [stateFilter, setStateFilter] = useState("");
  const [q, setQ] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [selected, setSelected] = useState<string | null>(null);
  const [themeIdx, setThemeIdx] = useState(0);
  const [now, setNow] = useState(fetchedAt);
  const [trialLimit, setTrialLimit] = useState(100);
  const [busy, setBusy] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Record<string, Msg>>({});

  // 15초마다 서버 컴포넌트를 다시 그려 BE 를 다시 읽는다(콘솔 tick 과 같은 주기).
  useEffect(() => {
    const timer = window.setInterval(() => { router.refresh(); setNow(Date.now() / 1000); }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [router]);

  // 주소 끝 #site=<사이트 ID> 로 그 사이트를 바로 연다. 저장된 화면 밝기를 되살린다.
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- 브라우저에서만 읽을 수 있는 값을 첫 그림 뒤에 반영 */
    if (location.hash.startsWith("#site=")) { setSelected(decodeURIComponent(location.hash.slice(6))); setOnlyAttn(false); }
    try { const i = THEMES.findIndex(([v]) => v === localStorage.getItem("console-theme")); if (i >= 0) setThemeIdx(i); } catch { /* 저장소 없음 */ }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  const reset = () => setShown(PAGE);
  const counts = countStates(all);
  const clsCounts = countClasses(all);
  const approvalN = all.filter((s) => s.approval).length;
  const uncollectedN = all.filter((s) => s.uncollected).length;
  const stopped = all.filter((s) => s.state === "stop");
  const lockedN = all.filter((s) => s.state === "lock").length;
  // 위쪽에는 사이트별 줄 대신 요약 한 줄: 누르면 왼쪽 목록이 그 상태로 걸러진다. 이유는 상세 패널에.
  const showState = (state: "stop" | "lock") => {
    setStateFilter(state); setOnlyAttn(false); setSelType(""); setSelCountry(""); setSelCls(""); setLane(""); setSpecial(""); reset();
  };

  useEffect(() => {
    document.title = (stopped.length ? `⚠ ${stopped.length}건 확인 필요 · ` : "") + "수집 콘솔";
  }, [stopped.length]);

  const primaryField = view === "type" ? "type" : "country";
  const primary = view === "type" ? selType : selCountry;
  const scope: Scope = { attn: onlyAttn, type: selType, country: selCountry, cls: selCls, lane, special };
  const sc = scoped(all, scope);
  const rows = useMemo(() => sortSites(filtered(all, { ...scope, state: stateFilter, q })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [all, onlyAttn, selType, selCountry, selCls, lane, special, stateFilter, q]);
  const lanes = useMemo(() => laneNames(all), [all]);
  const current = all.find((s) => s.id === selected) ?? rows[0] ?? all[0] ?? null;

  const heading = [onlyAttn ? "주의 필요" : null, special ? SPECIAL_LABEL[special] : null, selCls ? CLASSES[selCls as keyof typeof CLASSES].label : null,
    selType || null, selCountry || null, lane ? `구역 ${lane}` : null].filter(Boolean).join(" · ") || "전체 사이트";

  const pick = (id: string) => {
    setSelected(id);
    try { history.replaceState(null, "", `#site=${encodeURIComponent(id)}`); } catch { /* 무시 */ }
    if (matchMedia("(max-width:1180px)").matches) document.getElementById("cc-detail")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const railPick = (key: string) => {
    setSelType(""); setSelCountry(""); setSpecial("");
    if (key === "__attn") setOnlyAttn(true);
    else {
      setOnlyAttn(false);
      if (key === "__approval") setSpecial("approval");
      else if (key === "__uncollected") setSpecial("uncollected");
      else if (key !== "__all") { if (view === "type") setSelType(key); else setSelCountry(key); }
    }
    reset();
  };
  const toggleSpecial = (value: Exclude<Special, "">) => { setSpecial(special === value ? "" : value); if (special !== value) setOnlyAttn(false); reset(); };

  const act = async (site: ConsoleSite, action: CollectAction) => {
    setBusy(site.id);
    setMsgs((m) => ({ ...m, [site.id]: { ok: true, t: "요청 보내는 중…" } }));
    const outcome = await performAction(action, { confirm: (text) => window.confirm(text), fetch: (...args) => fetch(...args) });
    setBusy(null);
    if (outcome.kind === "skipped") { setMsgs((m) => { const n = { ...m }; delete n[site.id]; return n; }); return; }
    setMsgs((m) => ({ ...m, [site.id]: { ok: outcome.kind === "ok", t: outcome.message } }));
    if (outcome.kind === "rejected" && outcome.reason === "large_needs_approval") setHeld((h) => h.includes(site.id) ? h : [...h, site.id]);
    router.refresh();
  };

  const theme = THEMES[themeIdx];
  const nextTheme = () => {
    const i = (themeIdx + 1) % THEMES.length;
    setThemeIdx(i);
    try { localStorage.setItem("console-theme", THEMES[i][0]); } catch { /* 무시 */ }
  };

  const errors = [
    !data.sitesOk && "사이트 수집 현황을 불러올 수 없습니다(/collect/sites) — BE 연결·인증 또는 배포 버전을 확인하세요",
    data.sitesOk && !data.statusOk && "판정 파일(site-status) 폴더를 읽지 못했습니다 — 구분 값이 비어 있거나 오래됐을 수 있습니다",
    data.sitesOk && !data.documentsOk && "DB 문서 수를 읽지 못했습니다",
    !data.runsOk && "실행기 작업 목록(/collect/runs)을 읽지 못했습니다",
    !data.progressOk && "백필 진행(/progress)을 읽지 못했습니다",
    data.sitesOk && data.directOk === false && "직접 실행 진행(O1 상태 파일)을 읽지 못했습니다 — 직접 실행 중인 사이트가 '수집 중'으로 보이지 않을 수 있습니다",
    !data.uncollectedOk && "미수집 사유(/collect/uncollected)를 읽지 못했습니다",
    !data.stats && "DB 통계(/stats)를 읽지 못했습니다",
  ].filter(Boolean).join(" · ");

  const runner = data.runner;
  const runnerMeta = data.runnerOk
    ? `실행기 정상${runner?.paused ? " · 일시 중지" : ""}${runner?.running !== null && runner?.running !== undefined ? ` · 실행 ${runner.running} · 대기 ${runner.queued ?? 0} · 동시 ${runner.max_parallel ?? "-"}` : ""}`
    : `실행기 연결 안 됨 (${data.runnerText})`;

  return <div className="cc" data-theme={theme[0] === "auto" ? undefined : theme[0]}>
    <div className="wrap">
      <div className="top">
        <div>
          <h1>수집 콘솔</h1>
          <div className="meta">
            <span>마지막 조회 {ago(now - fetchedAt)} · 15초마다 갱신 · {runnerMeta}
              {data.stats ? ` · DB 문서 ${fmt(data.stats.documents)}건 · 사이트 ${fmt(data.stats.sites)}곳` : ""}
              {data.taxonomyVersion ? ` · 분류 ${data.taxonomyVersion}` : ""}</span>
            {" · "}<Link href="/">작업 대시보드</Link>
          </div>
        </div>
        <div className="ctl">
          <div className="seg" role="group" aria-label="보기 방식">
            {([["type", "유형별"], ["country", "국가별"], ["matrix", "유형 × 국가"]] as const).map(([v, label]) =>
              <button key={v} type="button" className={view === v ? "on" : ""} onClick={() => { setView(v); setSelType(""); setSelCountry(""); reset(); }}>{label}</button>)}
          </div>
          <button type="button" onClick={() => { router.refresh(); setNow(Date.now() / 1000); }}>새로고침</button>
          <button type="button" aria-label="화면 밝기 전환" onClick={nextTheme}>화면: {theme[1]}</button>
        </div>
      </div>
      <div className="err" role="alert">{errors}</div>
      <div className="sum" aria-label="상태 요약">
        {STATE_KEYS.map((k) => <div key={k} className={`chip ${k === "stop" && counts[k] ? "bad" : ""}`}><b>{fmt(counts[k])}</b><span>{STATES[k].label}</span></div>)}
        <button type="button" className={`chip ${approvalN ? "warn" : ""} ${special === "approval" ? "on" : ""}`} aria-pressed={special === "approval"} onClick={() => toggleSpecial("approval")}><b>{fmt(approvalN)}</b><span>승인 대기</span></button>
        <button type="button" className={`chip ${special === "uncollected" ? "on" : ""}`} aria-pressed={special === "uncollected"} onClick={() => toggleSpecial("uncollected")}><b>{fmt(uncollectedN)}</b><span>미수집</span></button>
      </div>
      <div className="csum" aria-label="실행 구분">
        <span className="note">구분</span>
        {CLASS_KEYS.filter((k) => clsCounts[k] || (k !== "skip" && k !== "none")).map((k) =>
          <button key={k} type="button" className={selCls === k ? "on" : ""} aria-pressed={selCls === k}
            onClick={() => { const v = selCls === k ? "" : k; setSelCls(v); if (v) setOnlyAttn(false); reset(); }}>
            <span className={CLASSES[k].tone}>●</span> {CLASSES[k].label}<b>{fmt(clsCounts[k])}</b></button>)}
        <span className="note">근거: 실행 기록 &gt; site-status{data.latestJudgment ? ` · 판정 파일 최근 ${ago(now - data.latestJudgment)}` : ""}</span>
      </div>
      <div className="alerts" role="status">
        {(stopped.length > 0 || lockedN > 0) && <div className="alert summary">확인 필요
          {stopped.length > 0 && <button type="button" onClick={() => showState("stop")}>멈춤 {fmt(stopped.length)}곳</button>}
          {lockedN > 0 && <button type="button" onClick={() => showState("lock")}>접근 제한 {fmt(lockedN)}곳</button>}
          <small>누르면 목록이 걸러집니다 · 사이트별 이유는 오른쪽 상세에</small></div>}
        {!data.runnerOk && <div className="alert warn" role="alert">실행기 없음 · 시작·이어받기 버튼을 막아 두었습니다<small>{data.runnerText}</small></div>}
        {data.runnerOk && runner?.paused && <div className="alert warn" role="alert">실행기 일시 중지 · 새 작업 요청은 거절됩니다</div>}
      </div>
      <p className="info">상태는 운영 BE 의 실행기 작업 기록(/collect)·백필 진행(/progress)·사이트 판정 파일(site-status)에서 읽습니다. <b>미시작</b>은 &ldquo;실행기·백필 기록 없음&rdquo;이라는 뜻입니다. 시작·이어받기는 실행기에 등록된 plan 으로만 실행하며, 예상 1만 건 이상(또는 미상)인 이어받기는 대량 승인이 있어야 받습니다(<b>승인 대기</b>).</p>

      <div className={`main ${view === "matrix" ? "nr" : ""}`}>
        {view !== "matrix" && <nav className="panel rail" aria-label="분류">
          <RailItem label="주의 필요" list={all.filter(needsAttention)} on={onlyAttn && !primary && !special} onClick={() => railPick("__attn")} />
          <hr />
          <RailItem label="전체" list={all} on={!onlyAttn && !primary && !special} onClick={() => railPick("__all")} />
          <RailItem label="승인 대기" list={all.filter((s) => s.approval)} on={special === "approval" && !primary} onClick={() => railPick("__approval")} />
          <RailItem label="미수집" list={all.filter((s) => s.uncollected)} on={special === "uncollected" && !primary} onClick={() => railPick("__uncollected")} />
          <hr />
          {namesBy(all, primaryField).map((n) => {
            const l = all.filter((s) => s[primaryField] === n);
            const a = l.filter(needsAttention).length;
            return <RailItem key={n} label={n} list={l} on={primary === n} extra={a ? <div className="warn">주의 {a}</div> : null} onClick={() => railPick(n)} />;
          })}
        </nav>}

        <section className="panel" id="cc-list" aria-labelledby="cc-list-h">
          <h2 id="cc-list-h">{heading} · {fmt(sc.length)}곳</h2>
          {view === "matrix" && <Matrix list={all} selType={selType} selCountry={selCountry}
            onType={(t) => { setSelType(selType === t ? "" : t); reset(); }}
            onCountry={(c) => { setSelCountry(selCountry === c ? "" : c); reset(); }}
            onCell={(c, t) => { const same = selCountry === c && selType === t; setSelCountry(same ? "" : c); setSelType(same ? "" : t); reset(); }} />}
          <div className="cc-filters">
            <label className="note"><input type="checkbox" checked={onlyAttn} onChange={(e) => { setOnlyAttn(e.target.checked); reset(); }} /> 주의 필요만</label>
            <input type="search" value={q} onChange={(e) => { setQ(e.target.value); reset(); }} placeholder="사이트 ID 검색" aria-label="검색" />
            <select value={stateFilter} aria-label="상태 필터" onChange={(e) => { setStateFilter(e.target.value); reset(); }}>
              <option value="">모든 상태</option>
              {STATE_KEYS.map((k) => <option key={k} value={k}>{STATES[k].label} ({sc.filter((s) => s.state === k).length})</option>)}
            </select>
            <select value={selCls} aria-label="구분 필터" onChange={(e) => { setSelCls(e.target.value); if (e.target.value) setOnlyAttn(false); reset(); }}>
              <option value="">모든 구분</option>
              {CLASS_KEYS.map((k) => <option key={k} value={k}>{CLASSES[k].label}</option>)}
            </select>
            <select value={lane} aria-label="구역 필터" disabled={!lanes.length} onChange={(e) => { setLane(e.target.value); if (e.target.value) setOnlyAttn(false); reset(); }}>
              <option value="">{lanes.length ? "모든 구역" : "구역 정보 없음"}</option>
              {lanes.map((name) => <option key={name} value={name}>구역 {name}</option>)}
            </select>
          </div>
          {view !== "matrix" && <SecondaryChips view={view} base={all.filter((s) => (!onlyAttn || needsAttention(s)) && (!primary || s[primaryField] === primary)
            && (special !== "approval" || s.approval) && (special !== "uncollected" || s.uncollected))}
            value={view === "type" ? selCountry : selType}
            onPick={(v) => { if (view === "type") setSelCountry(v); else setSelType(v); reset(); }} />}
          <div className="tablewrap">
            <table>
              <thead><tr><th>사이트</th><th>상태</th><th style={{ width: "34%" }}>저장 / 사이트 총건수</th><th className="r">마지막 활동</th></tr></thead>
              <tbody>
                {rows.slice(0, shown).map((s) => <SiteRow key={s.id} site={s} now={now} sel={s.id === current?.id} onPick={() => pick(s.id)} />)}
                {!rows.length && <tr><td colSpan={4} className="note" style={{ padding: 16 }}>조건에 맞는 사이트가 없습니다.</td></tr>}
              </tbody>
            </table>
          </div>
          {rows.length > shown && <button type="button" className="more" onClick={() => setShown(shown + PAGE)}>더 보기 ({fmt(rows.length - shown)}곳 남음)</button>}
          <p className="note" style={{ marginTop: 10 }}>10만 건이 넘는 사이트만 진행 막대를 보여 주고 나머지는 한 줄로 줄입니다. 총건수를 모르면 비율을 만들지 않습니다.</p>
        </section>

        <aside className="panel sec" id="cc-detail" aria-live="polite">
          {current ? <Detail site={current} data={data} now={now} trialLimit={trialLimit} setTrialLimit={setTrialLimit}
            busy={busy === current.id} anyBusy={busy !== null} msg={msgs[current.id]} onAction={(a) => act(current, a)} />
            : <p className="note">사이트를 고르세요.</p>}
        </aside>
      </div>
    </div>
  </div>;
}

function RailItem({ label, list, on, extra, onClick }: Readonly<{ label: string; list: readonly ConsoleSite[]; on: boolean; extra?: ReactNode; onClick: () => void }>) {
  return <button type="button" className={`t ${on ? "on" : ""}`} onClick={onClick}>
    <div className="tn"><span>{label}</span><em>{fmt(list.length)}</em></div>
    {list.length > 0 && <MixBar list={list} />}{extra}
  </button>;
}

function SecondaryChips({ view, base, value, onPick }: Readonly<{ view: "type" | "country"; base: readonly ConsoleSite[]; value: string; onPick: (v: string) => void }>) {
  const cs = secondaryCounts(base, view === "type" ? "country" : "type");
  return <div className="chips" aria-label="보조 필터">
    <button type="button" className={value === "" ? "on" : ""} onClick={() => onPick("")}>{view === "type" ? "모든 국가" : "모든 유형"}<em>{cs.length}</em></button>
    {cs.slice(0, 12).map(([c, n]) => <button type="button" key={c} className={value === c ? "on" : ""} onClick={() => onPick(c)}>{c}<em>{n}</em></button>)}
    {cs.length > 12 && <span className="note">외 {cs.length - 12}개 (왼쪽 목록이나 표 보기로 고르세요)</span>}
  </div>;
}

function Matrix({ list, selType, selCountry, onType, onCountry, onCell }: Readonly<{
  list: readonly ConsoleSite[]; selType: string; selCountry: string;
  onType: (t: string) => void; onCountry: (c: string) => void; onCell: (c: string, t: string) => void;
}>) {
  const types = namesBy(list, "type");
  const countries = namesBy(list, "country");
  return <div className="matrix"><table>
    <thead><tr><th><span className="sub">국가 ＼ 유형</span></th>
      {types.map((t) => <th key={t}><button type="button" className={selType === t ? "on" : ""} onClick={() => onType(t)}>{t}</button></th>)}<th>합계</th></tr></thead>
    <tbody>{countries.map((c) => {
      const row = list.filter((s) => s.country === c);
      const a = row.filter(needsAttention).length;
      return <tr key={c}>
        <td className="rh"><button type="button" className={selCountry === c ? "on" : ""} onClick={() => onCountry(c)}>{c}</button></td>
        {types.map((t) => {
          const l = row.filter((s) => s.type === t);
          const w = l.filter(needsAttention).length;
          const on = selCountry === c && selType === t;
          return <td key={t}><button type="button" className={`cell ${l.length ? "" : "z"} ${on ? "on" : ""} ${w ? "w" : ""}`} disabled={!l.length}
            aria-label={`${c} ${t} ${l.length}곳${w ? `, 주의 ${w}곳` : ""}`} onClick={() => onCell(c, t)}>{l.length || "·"}{w ? <b>!{w}</b> : null}</button></td>;
        })}
        <td className="num">{row.length}{a ? <b style={{ color: "var(--stop)", fontSize: 10, marginLeft: 3 }}>!{a}</b> : null}</td>
      </tr>;
    })}</tbody>
  </table></div>;
}

function SiteRow({ site: s, now, sel, onPick }: Readonly<{ site: ConsoleSite; now: number; sel: boolean; onPick: () => void }>) {
  const big = isBig(s);
  const pct = percentOf(s);
  return <tr className={`row ${big ? "" : "compact"} ${sel ? "sel" : ""}`} tabIndex={0} aria-label={`${s.name} 상세 보기`} onClick={onPick}
    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(); } }}>
    <td><div className="name">{s.name}</div><div className="sub">{s.country} · {s.type}{s.name !== s.id ? ` · ${s.id}` : ""}</div></td>
    <td><Badge state={s.state} />
      <div style={{ marginTop: 3, display: "flex", gap: 4, flexWrap: "wrap" }}>
        <ClsPill site={s} />{s.why && <span className="pill">{s.why}</span>}{s.approval && <span className="pill ap">승인 대기</span>}
      </div></td>
    <td>{big ? <>
      <div className="num">{fmt(s.saved)} / {s.total ? fmt(s.total) : "총건수 미확인"}{pct !== null ? ` · ${pctText(pct)}%` : ""}</div>
      <div className={`bar ${pct === null ? "unk" : ""}`}><i style={{ width: `${pct ?? 100}%` }} /></div>
    </> : <span className="num">{s.saved ? fmt(s.saved) : "-"}{s.total ? ` / ${fmt(s.total)}` : ""}</span>}</td>
    <td className="num r">{s.lastTs ? ago(now - s.lastTs) : "-"}</td>
  </tr>;
}

function Detail({ site: s, data, now, trialLimit, setTrialLimit, busy, anyBusy, msg, onAction }: Readonly<{
  site: ConsoleSite; data: ConsoleData; now: number; trialLimit: number; setTrialLimit: (n: number) => void;
  busy: boolean; anyBusy: boolean; msg: Msg | undefined; onAction: (a: CollectAction) => void;
}>) {
  const tot = s.total ?? 0;
  const left = tot ? Math.max(0, tot - s.saved - s.failed) : null;
  const seg = (v: number, color: string) => tot && v ? <i style={{ width: `${Math.min(100, (v / tot) * 100)}%`, background: color }} /> : null;
  const cur = s.runs.find((r) => ["queued", "running", "cancelling"].includes(r.status)) ?? s.runs[0] ?? null;
  const check = startCheck(s, data.runner, data.runnerOk, data.runnerText);
  const off = check.blocked.length > 0 || anyBusy;
  const resumeOff = off || check.resumeBlocked.length > 0;
  const pre: Array<{ ok: boolean; warn?: boolean; t: string; d: string }> = [];
  if (cur?.status === "running" || s.backfill?.active_status === "running") {
    pre.push({ ok: s.state !== "stop", t: "수집 작업이 움직이고 있다", d: s.state === "stop" ? s.alert ?? "갱신이 멈췄습니다" : "실행 중 · 마지막 활동 " + (s.lastTs ? ago(now - s.lastTs) : "-") });
  }
  pre.push({ ok: s.state !== "run", t: "같은 사이트 작업이 실행 중이 아니다", d: s.state === "run" ? "이미 실행 중입니다" : s.state === "stop" ? "멈춘 실행이 있습니다. 이어받기로 재시작 대상입니다" : "실행 중인 작업 없음", warn: s.state === "stop" });
  const blockedBy = accessBlock(s);
  pre.push({ ok: !blockedBy, t: "사이트가 접근을 허용한다",
    d: blockedBy ? `${blockedBy} — 화면에서 해제하거나 우회·재시도할 수 없습니다` : cur ? "마지막 실행에서 접근 제한 없음" : "아직 확인 전 (실행 기록 없음)",
    warn: !cur && !blockedBy });
  if (cur && s.state === "review") pre.push({ ok: false, warn: true, t: "마지막 실행이 깨끗하게 끝났다", d: s.why || RUNNER_STATUS_KO[cur.status] || cur.status });
  pre.push({ ok: !s.approval, warn: s.approval, t: "대량 수집 승인이 필요 없다",
    d: s.approval ? "승인 대기 — 이어받기(전체)는 대량 승인 전까지 거절됩니다. 시험 수집은 됩니다" : s.plan ? (s.plan.approved_large ? "대량 승인 받음" : `예상 ${fmt(s.plan.expected_items)}건 — 승인 기준(1만 건) 미만`) : "plan 없음" });

  return <>
    <div>
      <h2 style={{ wordBreak: "break-all" }}>{s.name}</h2>
      <Badge state={s.state} /> <ClsPill site={s} />{s.approval && <> <span className="pill ap">승인 대기</span></>}
      <span className="sub" style={{ marginLeft: 8 }}>{s.country} · {s.type}</span>
      <div className="sub" style={{ wordBreak: "break-all" }}>{s.id} · <Link className="lnk" href={`/crawlers/${encodeURIComponent(s.id)}`}>수집기 상세</Link> · <Link className="lnk" href={crawlerDocumentsHref(s.id)}>문서</Link></div>
      {s.alert && <div className="err" style={{ marginTop: 4 }}>{s.alert}</div>}
      <div className="sub" style={{ marginTop: 4 }}>{STATES[s.state].label}{s.why ? ` · ${s.why}` : ""}</div>
      {s.runs[0] && <div className="sub">실행기 마지막 작업 — {runExplain(s.runs[0])}</div>}
      {s.direct && s.direct.run_id !== s.runs[0]?.run_id && <div className="sub" style={{ wordBreak: "break-word" }}>
        O1 상태 ({s.direct.status}{s.direct.finished_at && !s.direct.running ? ` · ${s.direct.finished_at}` : ""}) — {s.direct.detail}</div>}
      <div className="sub" style={{ marginTop: 4 }}><b className={CLASSES[s.cls.k].tone}>{classText(s.cls)}</b> · {s.cls.why}<br />근거 {s.cls.src}{s.cls.lane ? ` · 담당 구역 ${s.cls.lane}` : ""}</div>
    </div>
    <div>
      <h2>커버리지</h2>
      {tot ? <>
        <div className="cov" role="img" aria-label={`받은 ${fmt(s.saved)}건, 영구 실패 ${fmt(s.failed)}건, 남은 ${fmt(left)}건`}>{seg(s.saved, "var(--run)")}{seg(s.failed, "var(--review)")}{seg(left ?? 0, "var(--bar)")}</div>
        <div className="legend"><span style={{ "--c": "var(--run)" } as CSSProperties}>받은 것</span><span style={{ "--c": "var(--review)" } as CSSProperties}>영구 실패</span><span style={{ "--c": "var(--bar)" } as CSSProperties}>아직 못 받음</span></div>
      </> : <>
        <div className="cov" role="img" aria-label="총건수 미확인"><i style={{ width: "100%", background: "var(--lock)", opacity: 0.35 }} /></div>
        <div className="note">사이트 총건수 미확인 — 비율을 만들지 않습니다.</div>
      </>}
      <dl className="kv" style={{ marginTop: 8 }}>
        <dt>받은 것</dt><dd><span className="num">{fmt(s.saved)}</span>{s.savedSrc && <div className="sub">{s.savedSrc}</div>}</dd>
        <dt>사이트 총건수</dt><dd><span className="num">{tot ? fmt(tot) : "미확인"}</span>{s.totalSrc && <div className="sub">{s.totalSrc}</div>}</dd>
        <dt>아직 못 받음</dt><dd className="num">{left === null ? "-" : fmt(left)}</dd>
        <dt>영구 실패</dt><dd className="num">{fmt(s.failed)}</dd>
        <dt>DB 문서</dt><dd className="num">{s.documents === null ? "-" : fmt(s.documents)}</dd>
        <dt>마지막 활동</dt><dd className="num">{s.lastTs ? ago(now - s.lastTs) : "-"}</dd>
        {s.backfill && <><dt>백필</dt><dd className="num">{s.backfill.active_status ? RUNNER_STATUS_KO[s.backfill.active_status] ?? s.backfill.active_status : s.backfill.completed_at ? "완주" : s.backfill.has_cursor ? "중단됨" : "시작 전"}{s.backfill.active_job_id !== null ? ` · 작업 #${s.backfill.active_job_id}` : ""}</dd></>}
      </dl>
    </div>
    <hr />
    <div>
      <h2>미수집 사유</h2>
      {s.uncollected ? <>
        <div>{s.uncollected.reasons.length ? s.uncollected.reasons.map((r) => <span key={r} className="pill" style={{ marginRight: 4 }}>{r}</span>) : <span className="note">사유 코드 없음</span>}</div>
        {s.uncollected.note && <p className="note" style={{ margin: "6px 0 0" }}>메모: {s.uncollected.note}</p>}
      </> : <p className="note" style={{ margin: 0 }}>{data.uncollectedOk ? "미수집 목록에 없습니다 (실행 가능 판정이고 DB 문서가 있음)." : "미수집 사유를 읽지 못했습니다."}</p>}
      {s.status && <div className="sub" style={{ marginTop: 6 }}>판정 {stateLabel(s.status.state)}{s.status.detail ? ` · ${s.status.detail}` : ""}
        {s.status.codes.length > 0 && <> · <code>{s.status.codes.join(", ")}</code></>}</div>}
    </div>
    <hr />
    <div>
      <h2>실행 기록</h2>
      {s.runs.length ? <>
        <ul className="runs">{s.runs.slice(0, 8).map((x) => <li key={x.run_id}>
          <div className="num rn">{x.run_id} · {RUNNER_STATUS_KO[x.status] ?? x.status}{x.mode ? ` · ${runModeLabel(x.mode)}` : ""}{x.limit ? ` ${fmt(x.limit)}건` : ""}{x.status === "running" ? <> <span className="pulse" /></> : null}</div>
          {x.result && <div>{x.result.site_status ? RESULT_STATUS_KO[x.result.site_status] ?? x.result.site_status : "결과"} · 저장 <span className="num">{fmt(x.result.saved)}</span>{x.result.failed ? <> · 실패 <span className="num">{fmt(x.result.failed)}</span></> : null}{x.result.error ? ` · ${x.result.error}` : ""}</div>}
          <div className="sub rn">{x.requested_by ?? "-"} · {x.started_at ?? x.requested_at ?? "-"} → {x.finished_at ?? ""}{x.run_folder ? ` · ${x.run_folder}` : ""}</div>
          {(x.reason || x.result?.stop_reason) && <div className="sub rn">{[x.reason, x.result?.stop_reason].filter(Boolean).join(" · ").slice(0, 160)}</div>}
          {(x.status === "queued" || x.status === "running") && <button type="button" style={{ marginTop: 4 }} disabled={anyBusy || !data.runnerOk}
            onClick={() => onAction({ kind: "cancel", siteId: s.id, runId: x.run_id })}>취소</button>}
        </li>)}</ul>
        <p className="note" style={{ margin: "6px 0 0" }}>맨 위(실행 중이거나 가장 최근) 실행을 이 사이트의 현재 상태로 씁니다.</p>
      </> : <p className="note" style={{ margin: 0 }}>{data.runsOk ? "실행기에 이 사이트 작업 기록이 없습니다." : "실행기 작업 목록을 읽지 못했습니다."}</p>}
    </div>
    <hr />
    <div>
      <h2>시작 전 점검</h2>
      <ul className="pre">{pre.map((p) => <li key={p.t} className={p.warn ? "wn" : p.ok ? "ok" : "no"}><span className="mk">{p.warn ? "!" : p.ok ? "✓" : "✕"}</span><span>{p.t}<small>{p.d}</small></span></li>)}</ul>
      <div className="start">
        <button type="button" className="primary" disabled={resumeOff} onClick={() => onAction({ kind: "resume", siteId: s.id })}>{s.runs.length ? "이어받기 시작" : "수집 시작"}</button>
        <button type="button" disabled={off} onClick={() => onAction({ kind: "trial", siteId: s.id, limit: trialLimit })}>시험 수집</button>
        <label className="note">건수 <input type="number" min={1} max={TRIAL_LIMIT_MAX} value={trialLimit} disabled={off}
          onChange={(e) => { const v = Number.parseInt(e.target.value, 10); if (v > 0) setTrialLimit(Math.min(TRIAL_LIMIT_MAX, v)); }} /></label>
      </div>
      {check.blocked.length || check.resumeBlocked.length
        ? <ul className="pre" style={{ marginTop: 8 }}>
          {check.blocked.map((t) => <li key={t} className="no"><span className="mk">✕</span><span>{t}</span></li>)}
          {check.resumeBlocked.map((t) => <li key={t} className="wn"><span className="mk">!</span><span>{t}</span></li>)}
        </ul>
        : <p className="note" style={{ margin: "6px 0 0" }}>plan: 전략 {s.planInfo?.strategy ?? "-"} · 폴더 {s.planInfo?.folder ?? "-"} · 예상 {fmt(s.plan?.expected_items)}건. 누른 사람·시각은 실행기에 기록됩니다.</p>}
      {busy && <div className="ok-msg" style={{ marginTop: 6 }}>요청 보내는 중…</div>}
      {msg && !busy && <div role={msg.ok ? "status" : "alert"} className={msg.ok ? "ok-msg" : "why"} style={{ marginTop: 6 }}>{msg.t}</div>}
      <p className="note" style={{ margin: "8px 0 0" }}>접근 제한(robots 거부, 로그인, 챌린지)은 이 화면에서 해제하거나 우회할 수 없습니다.</p>
    </div>
    {tsOf(s.status?.updated_at) !== null && <p className="note" style={{ margin: 0 }}>판정 파일 {s.status?.file} · {ago(now - (tsOf(s.status?.updated_at) ?? now))}</p>}
  </>;
}

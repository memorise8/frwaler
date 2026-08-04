import Link from "next/link"
import { getCollectionReport } from "../../lib/report"
import type { ReportBucket } from "../../lib/report"

export const dynamic = "force-dynamic"
export const metadata = { title: "수집 현황 보고" }

const AUDIT = { total: 832, success: 694, unmet: 138, date: "2026-07-20" } as const
const fmt = (n: number): string => n.toLocaleString("ko-KR")
const pct = (part: number, whole: number): number => whole > 0 ? Math.round((1000 * part) / whole) / 10 : 0
const rateVar = (r: number): string => r >= 70 ? "var(--good, #3f7350)" : r >= 40 ? "var(--warn, #9a7524)" : "var(--bad, #9e463b)"

// Scoped to .rp; inherits Libertree's global tokens (--canvas/--surface/--ink/--moss/…) and only
// adds tuned earthy semantic colors. Flat, square, light — matching the rest of the site.
const STYLE = `
.rp{--good:#3f7350;--warn:#9a7524;--bad:#9e463b;--neutral:#5f6f74;--good-wash:var(--moss-wash);--warn-wash:var(--sand-wash);--bad-wash:#f0e3df;--serif:"Noto Serif KR","Iowan Old Style","AppleMyungjo",serif;--mono:"SFMono-Regular",Consolas,monospace;display:flex;flex-direction:column;gap:var(--space-10)}
.rp *{box-sizing:border-box}
.rp>section{border-block-start:1px solid var(--rule);padding-block-start:var(--space-8)}
.rp .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.rp .rtop{display:flex;justify-content:space-between;align-items:flex-end;gap:var(--space-5);flex-wrap:wrap;border-block-end:1px solid var(--rule);padding-block-end:var(--space-5)}
.rp .rtop .head{display:flex;flex-direction:column;gap:var(--space-2)}
.rp .rtop h1{font-family:var(--serif);font-weight:600;font-size:clamp(1.9rem,4.5vw,2.9rem);line-height:1.06;letter-spacing:-0.035em;margin:0}
.rp .rtop .meta{text-align:right;font-size:0.8125rem;color:var(--muted);display:flex;flex-direction:column;gap:2px}
.rp .rtop .meta .num{color:var(--ink)}
.rp .live{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:0.72rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--moss)}
.rp .live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--moss)}
.rp section{display:flex;flex-direction:column;gap:var(--space-5)}
.rp .sh{display:flex;align-items:end;justify-content:space-between;gap:var(--space-4);flex-wrap:wrap}
.rp .sh-head{display:flex;flex-direction:column;gap:var(--space-2)}
.rp .sh h2{font-family:var(--serif);font-weight:600;font-size:1.45rem;letter-spacing:-0.03em;margin:0}
.rp .sh p{margin:0;font-size:0.875rem;color:var(--muted);max-inline-size:52ch;word-break:keep-all}
.rp .kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:var(--space-3)}
@media (max-width:54rem){.rp .kpis{grid-template-columns:repeat(2,1fr)}}
.rp .tile{background:var(--surface);border:1px solid var(--rule);padding:var(--space-4);display:flex;flex-direction:column;gap:6px}
.rp .tile .kl{font-size:0.8rem;color:var(--muted);font-weight:600}
.rp .tile .kv{font-family:var(--serif);font-size:clamp(1.5rem,3.2vw,1.95rem);font-weight:600;line-height:1;letter-spacing:-0.03em;font-variant-numeric:tabular-nums}
.rp .tile .ks{font-size:0.72rem;color:var(--muted)}
.rp .tile.acc{background:var(--moss-wash)}
.rp .tile.acc .kv{color:var(--moss)}
.rp .g2{display:grid;grid-template-columns:1.15fr .85fr;gap:var(--space-4)}
@media (max-width:52rem){.rp .g2{grid-template-columns:1fr}}
.rp .panel{background:var(--surface);border:1px solid var(--rule);padding:var(--space-6)}
.rp .panel h3{margin:0 0 4px;font-size:0.95rem;font-weight:700}
.rp .panel .hint{margin:0 0 var(--space-5);font-size:0.8rem;color:var(--muted);word-break:keep-all}
.rp .segbar{display:flex;flex-direction:row;gap:0;height:2.9rem;overflow:hidden;border:1px solid var(--rule)}
.rp .segbar>span{display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:0.8rem;font-weight:600;color:var(--surface);white-space:nowrap;min-width:0;overflow:hidden}
.rp .segbar>span+span{border-inline-start:2px solid var(--canvas)}
.rp .legend{display:flex;gap:var(--space-5);margin-block-start:var(--space-4);flex-wrap:wrap}
.rp .legend span{display:inline-flex;align-items:center;gap:7px;font-size:0.8rem;color:var(--muted)}
.rp .dot{width:11px;height:11px;flex:none}
.rp .figs{display:flex;gap:var(--space-6);margin-block-start:var(--space-5)}
.rp .figs>div{flex:1}
.rp .fbig{font-family:var(--serif);font-size:1.6rem;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
.rp .flab{font-size:0.78rem;color:var(--muted);margin-block-start:5px;word-break:keep-all}
.rp .dwrap{display:flex;align-items:center;gap:var(--space-5)}
.rp .donut{width:8rem;height:8rem;flex:none;border-radius:50%;display:grid;place-items:center}
.rp .donut .din{width:5.7rem;height:5.7rem;background:var(--surface);border-radius:50%;display:grid;place-items:center;text-align:center}
.rp .dpct{font-family:var(--serif);font-size:1.5rem;font-weight:600;line-height:1;font-variant-numeric:tabular-nums}
.rp .dcap{font-size:0.66rem;color:var(--muted);margin-block-start:3px}
.rp .cov{flex:1;display:flex;flex-direction:column;gap:var(--space-3)}
.rp .covr{display:flex;align-items:center;gap:10px;font-size:0.82rem}
.rp .covr .num{margin-inline-start:auto;font-weight:600}
.rp .covr .sw{width:11px;height:11px;flex:none}
.rp .bars{display:flex;flex-direction:column;gap:var(--space-3)}
.rp .bar{display:grid;grid-template-columns:5.2rem 1fr 7.4rem;align-items:center;gap:var(--space-3)}
@media (max-width:36rem){.rp .bar{grid-template-columns:4rem 1fr 6rem;gap:var(--space-2)}}
.rp .blab{font-size:0.82rem;font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp .btrk{position:relative;height:1.65rem;background:var(--canvas);border:1px solid var(--rule);overflow:hidden}
.rp .bfill{position:absolute;inset:0 auto 0 0;display:flex;align-items:center;padding-inline-start:8px;color:var(--surface);font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:0.72rem;font-weight:600}
.rp .bmeta{font-size:0.75rem;color:var(--muted);font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.rp .bmeta b{color:var(--ink);font-weight:600}
.rp .funnel{display:grid;grid-template-columns:repeat(5,1fr);gap:var(--space-2)}
@media (max-width:45rem){.rp .funnel{grid-template-columns:1fr 1fr}}
.rp .step{background:var(--surface);border:1px solid var(--rule);padding:var(--space-4)}
.rp .step .sstage{font-size:0.78rem;color:var(--muted);font-weight:600}
.rp .step .snum{font-family:var(--serif);font-size:clamp(1.25rem,2.6vw,1.6rem);font-weight:600;font-variant-numeric:tabular-nums;margin-block-start:6px;line-height:1}
.rp .step .spct{font-family:var(--mono);font-size:0.72rem;margin-block-start:7px;display:inline-block;padding:2px 7px;font-variant-numeric:tabular-nums}
.rp .step .strk{height:5px;background:var(--canvas);margin-block-start:var(--space-3);overflow:hidden}
.rp .step .strk i{display:block;height:100%;background:var(--moss)}
.rp .tscroll{overflow-x:auto;border:1px solid var(--rule)}
.rp table{border-collapse:collapse;width:100%;min-width:34rem;font-size:0.82rem}
.rp thead th{text-align:left;font-family:var(--mono);font-size:0.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:var(--space-3) var(--space-4);border-block-end:1px solid var(--rule);background:var(--surface)}
.rp tbody td{padding:var(--space-3) var(--space-4);border-block-end:1px solid var(--rule)}
.rp tbody tr:last-child td{border-block-end:none}
.rp td.r,.rp th.r{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.rp .pill{display:inline-block;padding:1px 8px;font-size:0.72rem;font-weight:600;font-family:var(--mono)}
.rp .pill.bad{background:var(--bad-wash);color:var(--bad, #9e463b)}
.rp .pill.warn{background:var(--warn-wash);color:var(--warn, #9a7524)}
.rp .pill.good{background:var(--good-wash);color:var(--good, #3f7350)}
.rp .tag{font-size:0.72rem;color:var(--muted)}
.rp .callout{background:var(--moss-wash);border:1px solid var(--rule);padding:var(--space-5) var(--space-6);font-size:0.86rem;word-break:keep-all}
.rp .callout b{color:var(--moss)}
.rp .callout ul{margin:var(--space-3) 0 0;padding-inline-start:1.1rem}
.rp .callout li{margin:5px 0}
.rp .note{border-inline-start:3px solid var(--warn, #9a7524);background:var(--sand-wash);padding:var(--space-3) var(--space-4);font-size:0.82rem;margin-block-start:var(--space-4);word-break:keep-all}
.rp .foot{border-block-start:1px solid var(--rule);font-family:var(--mono);font-size:0.72rem;color:var(--muted);display:flex;justify-content:space-between;gap:var(--space-4);flex-wrap:wrap;padding-block-start:var(--space-5)}
`

const Bar = ({ b }: { readonly b: ReportBucket }): React.JSX.Element => {
  const w = Math.max(b.rate, 3)
  const inside = b.rate >= 16
  return (
    <div className="bar">
      <div className="blab" title={b.key}>{b.key}</div>
      <div className="btrk"><div className="bfill" style={{ width: `${w}%`, background: rateVar(b.rate) }}>{inside ? `${b.rate.toFixed(1)}%` : ""}</div></div>
      <div className="bmeta"><b>{fmt(b.recs)}</b> · {fmt(b.pdf)}{inside ? "" : ` · ${b.rate.toFixed(1)}%`}</div>
    </div>
  )
}

export default function ReportPage(): React.JSX.Element {
  const r = getCollectionReport()
  const t = r.total
  const notPdf = t.recs - t.pdf
  const cRate = pct(t.pdf + r.unmet.c, t.recs)
  const bcRate = pct(t.pdf + r.unmet.b + r.unmet.c, t.recs)
  const realRate = pct(t.pdf, t.recs - r.unmet.a)
  const coverage = pct(AUDIT.success, AUDIT.total)
  const funnel = [
    { stage: "발견 (메타)", n: t.recs, p: 100 },
    { stage: "원문(PDF) 확보", n: t.pdf, p: t.rate },
    { stage: "텍스트 추출", n: t.txt, p: pct(t.txt, t.recs) },
    { stage: "한글 번역", n: t.ko, p: pct(t.ko, t.recs) },
    { stage: "요약 생성", n: t.summary, p: pct(t.summary, t.recs) },
  ]
  const pill = (x: number): string => x < 20 ? "pill bad" : x < 50 ? "pill warn" : "pill good"
  return (
    <section className="rp">
      <style>{STYLE}</style>
      <div className="rtop">
        <div className="head">
          <p className="page-label">COLLECTION STATUS</p>
          <h1>수집 현황 보고</h1>
        </div>
        <div className="meta">
          <span className="live">Live · DB 실측</span>
          <span>대상 사이트 <span className="num">{fmt(t.sites)}</span> · 국가 <span className="num">{r.byCountry.length}</span></span>
          <span>커버리지 감사 <span className="num">{AUDIT.date}</span></span>
        </div>
      </div>

      <div className="kpis">
        <div className="tile"><span className="kl">문서 발견</span><span className="kv">{fmt(t.recs)}</span><span className="ks">메타데이터 수집</span></div>
        <div className="tile acc"><span className="kl">원문(PDF) 확보</span><span className="kv">{fmt(t.pdf)}</span><span className="ks">파일 다운로드 성공</span></div>
        <div className="tile"><span className="kl">원문 확보율</span><span className="kv">{t.rate.toFixed(1)}%</span><span className="ks">확보 ÷ 발견</span></div>
        <div className="tile"><span className="kl">한글 번역</span><span className="kv">{fmt(t.ko)}</span><span className="ks">발견 대비 {pct(t.ko, t.recs)}%</span></div>
        <div className="tile"><span className="kl">저장 용량</span><span className="kv">{t.sizeTb.toFixed(2)}<span style={{ fontSize: ".6em" }}> TB</span></span><span className="ks">원문 PDF 누적</span></div>
      </div>

      <section aria-label="성공 실패 요약">
        <div className="sh"><div className="sh-head"><p className="page-label">01 · OVERVIEW</p><h2>수집 성공 · 실패 요약</h2></div><p>① 발견한 문서의 원문을 확보했는가(원문 확보율) ② 대상 사이트를 열었는가(커버리지).</p></div>
        <div className="g2">
          <div className="panel">
            <h3>문서 원문 확보 (발견 {fmt(t.recs)}건 기준)</h3>
            <p className="hint">목록·메타는 확보했으나 실제 원문(PDF) 다운로드까지 성공한 비율.</p>
            <div className="segbar" role="img" aria-label={`확보 ${t.rate}%, 미확보 ${(100 - t.rate).toFixed(1)}%`}>
              <span style={{ width: `${t.rate}%`, background: "var(--good, #3f7350)" }}>확보 {t.rate.toFixed(1)}%</span>
              <span style={{ width: `${100 - t.rate}%`, background: "var(--bad, #9e463b)" }}>미확보 {(100 - t.rate).toFixed(1)}%</span>
            </div>
            <div className="figs">
              <div><div className="fbig num" style={{ color: "var(--good, #3f7350)" }}>{fmt(t.pdf)}</div><div className="flab">원문 확보 (성공)</div></div>
              <div><div className="fbig num" style={{ color: "var(--bad, #9e463b)" }}>{fmt(notPdf)}</div><div className="flab">원문 미확보</div></div>
              <div><div className="fbig num">{fmt(t.txt)}</div><div className="flab">텍스트 추출 ({pct(t.txt, t.recs)}%)</div></div>
            </div>
          </div>
          <div className="panel">
            <h3>대상 사이트 커버리지</h3>
            <p className="hint">감사 기준({AUDIT.date}) 대상 {AUDIT.total}개 사이트 중 실적.</p>
            <div className="dwrap">
              <div className="donut" style={{ background: `conic-gradient(var(--moss) 0 ${coverage}%, var(--sand-wash) ${coverage}% 100%)` }}>
                <div className="din"><div><div className="dpct">{coverage}%</div><div className="dcap">커버리지</div></div></div>
              </div>
              <div className="cov">
                <div className="covr"><span className="sw" style={{ background: "var(--moss)" }} />수집 성공 사이트<span className="num">{fmt(AUDIT.success)}</span></div>
                <div className="covr"><span className="sw" style={{ background: "var(--bad, #9e463b)" }} />미수집 사이트<span className="num">{fmt(AUDIT.unmet)}</span></div>
                <div className="covr"><span className="sw" style={{ background: "var(--ink)" }} />실제 적재 사이트(현재)<span className="num">{fmt(t.sites)}</span></div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section aria-label="유형별">
        <div className="sh"><div className="sh-head"><p className="page-label">02 · BY TYPE</p><h2>유형별 원문 확보율</h2></div><p>보고서·논문·연구자료는 높고, 보도자료는 낮음(대부분 원문 PDF가 없는 HTML 공지).</p></div>
        <div className="panel"><div className="bars">{r.byDocType.map((b) => <Bar key={b.key} b={b} />)}</div></div>
      </section>

      <section aria-label="국가별">
        <div className="sh"><div className="sh-head"><p className="page-label">03 · BY COUNTRY</p><h2>국가별 수집 규모 · 확보율</h2></div><p>막대 = 확보율, 오른쪽 = 발견 · 확보 건수. 상위 16개국.</p></div>
        <div className="panel"><div className="bars">{r.byCountry.slice(0, 16).map((b) => <Bar key={b.key} b={b} />)}</div></div>
      </section>

      <section aria-label="파이프라인">
        <div className="sh"><div className="sh-head"><p className="page-label">04 · PIPELINE</p><h2>처리 파이프라인</h2></div><p>발견 → 원문 확보 → 텍스트 추출 → 한글 번역 → 요약. 번역까지 원활, 요약은 초기 단계.</p></div>
        <div className="funnel">{funnel.map((s) => {
          const cls = s.p >= 55 ? "good" : s.p >= 30 ? "warn" : "bad"
          return (
            <div className="step" key={s.stage}>
              <div className="sstage">{s.stage}</div>
              <div className="snum">{fmt(s.n)}</div>
              <div className="spct" style={{ background: `var(--${cls}-wash)`, color: `var(--${cls})` }}>{s.p.toFixed(1)}%</div>
              <div className="strk"><i style={{ width: `${s.p}%` }} /></div>
            </div>
          )
        })}</div>
      </section>

      <section aria-label="미확보 상세">
        <div className="sh"><div className="sh-head"><p className="page-label">05 · UNMET</p><h2>미확보 {fmt(r.unmet.total)}건 — 왜 못 받았나</h2></div><p>미확보 = 제목·메타는 있으나 원문·본문 모두 없는 문서. 실패 지점으로 3분류.</p></div>
        <div className="g2">
          <div className="panel">
            <h3>실패 지점별 분류</h3>
            <p className="hint">원문 링크 유무 → 다운로드 성공 여부로 나눈 실측 분해.</p>
            <div className="segbar" role="img" aria-label="A B C 분류">
              <span style={{ width: `${pct(r.unmet.a, r.unmet.total)}%`, background: "var(--neutral, #5f6f74)" }}>A {pct(r.unmet.a, r.unmet.total)}%</span>
              <span style={{ width: `${pct(r.unmet.b, r.unmet.total)}%`, background: "var(--warn, #9a7524)" }}>B {pct(r.unmet.b, r.unmet.total)}%</span>
              <span style={{ width: `${pct(r.unmet.c, r.unmet.total)}%`, background: "var(--bad, #9e463b)" }}>C {pct(r.unmet.c, r.unmet.total)}%</span>
            </div>
            <div className="legend">
              <span><span className="dot" style={{ background: "var(--neutral, #5f6f74)" }} />A. 원문 링크 없음 · HTML 공지</span>
              <span><span className="dot" style={{ background: "var(--warn, #9a7524)" }} />B. 링크 추출 실패 · 문서형</span>
              <span><span className="dot" style={{ background: "var(--bad, #9e463b)" }} />C. 다운로드 실패 · 링크 있음</span>
            </div>
            <div className="figs">
              <div><div className="fbig num">{fmt(r.unmet.a)}</div><div className="flab">A · 원문 부재(추정)</div></div>
              <div><div className="fbig num" style={{ color: "var(--warn, #9a7524)" }}>{fmt(r.unmet.b)}</div><div className="flab">B · 재크롤 필요</div></div>
              <div><div className="fbig num" style={{ color: "var(--bad, #9e463b)" }}>{fmt(r.unmet.c)}</div><div className="flab">C · 링크 보유 · 재시도</div></div>
            </div>
          </div>
          <div className="panel">
            <h3>회수 여력 — 검증 수준 구분</h3>
            <p className="hint">확정된 것과 추정을 분리해 표기합니다.</p>
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", marginBlockStart: "6px" }}>
              <div>
                <div className="fbig num" style={{ color: "var(--good, #3f7350)" }}>+{fmt(r.unmet.c)}</div>
                <div className="flab"><b>검증된 회수 여력</b> (C, 링크 보유) → 확보율 {t.rate.toFixed(1)}% → <b>{cRate}%</b></div>
              </div>
              <div style={{ borderBlockStart: "1px solid var(--rule)", paddingBlockStart: "var(--space-3)" }}>
                <div className="fbig num" style={{ color: "var(--warn, #9a7524)" }}>+{fmt(r.unmet.b)}</div>
                <div className="flab"><b>미검증 상한</b> (B, 재크롤 필요) → 표본검증 후 최대 <b>{bcRate}%</b></div>
              </div>
            </div>
            <div className="note">A(원문 부재)를 확보율 지표에서 분리하면 <b>실질 확보율은 {realRate}%</b>. B 8.5만은 회수율 <b>미측정</b> — 표본(예: 200건) 재크롤로 확정 필요.</div>
          </div>
        </div>

        <div className="panel">
          <h3>유형별 미확보 분해 — 링크 없음 vs 다운로드 실패</h3>
          <p className="hint">보도자료는 대부분 원문 부재(A), 보고서는 링크 추출 실패가 커 회수 여지가 가장 큼.</p>
          <div className="tscroll">
            <table>
              <thead><tr><th>유형</th><th className="r">미확보</th><th className="r">링크 없음</th><th className="r">다운로드 실패</th></tr></thead>
              <tbody>{r.unmet.byType.map((u) => <tr key={u.key}><td>{u.key}</td><td className="r"><b>{fmt(u.unmet)}</b></td><td className="r">{fmt(u.noUrl)}</td><td className="r">{fmt(u.download)}</td></tr>)}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section aria-label="취약 구간">
        <div className="sh"><div className="sh-head"><p className="page-label">06 · WEAK SPOTS</p><h2>취약 구간 — 대량 발견 · 저확보 사이트</h2></div><p>발견은 많지만 원문 확보가 저조해 우선 개선이 필요한 출처(발견 1,000건↑ · 확보율 30% 미만).</p></div>
        <div className="tscroll">
          <table>
            <thead><tr><th>사이트</th><th>국가</th><th>유형</th><th className="r">발견</th><th className="r">확보</th><th className="r">확보율</th></tr></thead>
            <tbody>{r.weakSites.map((w) => <tr key={w.name}><td>{w.name}</td><td className="tag">{w.country}</td><td className="tag">{w.docType}</td><td className="r">{fmt(w.recs)}</td><td className="r">{fmt(w.pdf)}</td><td className="r"><span className={pill(w.rate)}>{w.rate.toFixed(1)}%</span></td></tr>)}</tbody>
          </table>
        </div>
        <div className="callout">
          <b>다음 조치 (검증 상태 구분)</b>
          <ul>
            <li><b>[검증됨] C 재시도</b> — 링크 보유 {fmt(r.unmet.c)}건. 실패가 소수 호스트(etera.ee 등)에 집중되어 rate-limit·재시도 튜닝으로 대응.</li>
            <li><b>[표본검증 필요] B 재크롤</b> — {fmt(r.unmet.b)}건은 상세페이지에 PDF가 실제 있는지 미확인. 200건 표본으로 회수율 측정 후 목표 확정.</li>
            <li><b>[정책] A 재분류</b> — 보도자료 {fmt(r.unmet.a)}건은 원문 부재로 정식 분류 + 본문 텍스트 별도 스크랩(현재 제목만 남는 문제 해소).</li>
          </ul>
        </div>
      </section>

      <div className="foot">
        <span>Libertree 수집 현황 · 내부 보고용 · <Link href="/search">자료 탐색</Link></span>
        <span>수치: libertree.db 실측 · 커버리지는 {AUDIT.date} 감사 스냅샷</span>
      </div>
    </section>
  )
}

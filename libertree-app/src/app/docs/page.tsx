import Link from "next/link"

export const metadata = { title: "산출물" }

type Deliverable = {
  readonly slug: string
  readonly title: string
  readonly docNo: string
  readonly summary: string
}

const DELIVERABLES: readonly Deliverable[] = [
  { slug: "completion-report", title: "완료보고", docNo: "2026-08-01", summary: "프로젝트 완료보고 — 수행 결과·수집 현황·후속 인수인계" },
  { slug: "functional-spec", title: "상세 기능정의서", docNo: "FUNC-2026-001", summary: "FR-01~10 기능 정의 및 구현 상태" },
  { slug: "wbs", title: "WBS · 작업분해구조", docNo: "WBS-2026-001", summary: "작업분해구조·일정·상태" },
  { slug: "dev-environment", title: "개발환경 구성 문서", docNo: "ENV-2026-001", summary: "언어·런타임·의존성·환경변수·실행 절차" },
  { slug: "infrastructure", title: "인프라 아키텍처 문서", docNo: "ARC-2026-001", summary: "시스템 구성도·컴포넌트·데이터 흐름·배포" },
  { slug: "api-spec", title: "API 명세서", docNo: "API-2026-001", summary: "엔드포인트 17종·인증·응답 규격" },
  { slug: "test-cases", title: "테스트 케이스", docNo: "TEST-2026-001", summary: "FR별 24 테스트 케이스·결과" },
  { slug: "beta-test-report", title: "베타테스트·디버깅 보고서", docNo: "BETA-2026-001", summary: "발견 이슈·PDF 회수·데이터 검증 실측" },
  { slug: "admin-manual", title: "관리자 매뉴얼", docNo: "MANUAL-2026-001", summary: "관리자 화면 9종 사용법·작업 절차" },
  { slug: "operations-guide", title: "시스템 운영 가이드", docNo: "OPS-2026-001", summary: "배포·크롤러 운영·장애 대응·모니터링" },
]

const STYLE = `
.dp{display:flex;flex-direction:column;gap:var(--space-8)}
.dp *{box-sizing:border-box}
.dp .dtop{display:flex;flex-direction:column;gap:var(--space-3);border-block-end:1px solid var(--rule);padding-block-end:var(--space-6)}
.dp .dtop h1{font-family:"Noto Serif KR","Iowan Old Style","AppleMyungjo",serif;font-weight:600;font-size:clamp(1.9rem,4.5vw,2.9rem);line-height:1.06;letter-spacing:-0.035em;margin:0}
.dp .dtop p{margin:0;color:var(--muted);font-size:0.9rem;max-inline-size:52ch;word-break:keep-all}
.dp .dlist{display:flex;flex-direction:column;gap:0;border:1px solid var(--rule);background:var(--surface)}
.dp .drow{display:flex;align-items:center;justify-content:space-between;gap:var(--space-4);padding:var(--space-5) var(--space-6);border-block-end:1px solid var(--rule);transition:background 150ms ease-out}
.dp .drow:last-child{border-block-end:none}
.dp .drow:hover{background:var(--moss-wash)}
.dp .drow .dhead{display:flex;flex-direction:column;gap:4px;min-inline-size:0}
.dp .drow h2{margin:0;font-size:1.05rem;font-weight:700;letter-spacing:-0.02em}
.dp .drow .dno{font-family:"SFMono-Regular",Consolas,monospace;font-size:0.72rem;color:var(--moss);letter-spacing:.04em}
.dp .drow .dsum{font-size:0.82rem;color:var(--muted);word-break:keep-all}
.dp .drow .darrow{flex:none;font-family:"SFMono-Regular",Consolas,monospace;font-size:0.8rem;color:var(--muted)}
.dp .drow:hover .darrow{color:var(--moss)}
`

export default function DocsPage(): React.JSX.Element {
  return (
    <section className="dp">
      <style>{STYLE}</style>
      <div className="dtop">
        <p className="page-label">DELIVERABLES</p>
        <h1>산출물</h1>
        <p>프로젝트 진행 중 작성된 산출물 문서입니다.</p>
      </div>
      <div className="dlist">
        {DELIVERABLES.map((d) => (
          <Link className="drow" href={`/docs/${d.slug}`} key={d.slug}>
            <div className="dhead">
              <span className="dno">{d.docNo}</span>
              <h2>{d.title}</h2>
              <span className="dsum">{d.summary}</span>
            </div>
            <span className="darrow">읽기 →</span>
          </Link>
        ))}
      </div>
    </section>
  )
}

# Session Progress — 2026-04-20

BJT/MOSFET 우주부품 스크리닝 서비스를 Cloudflare Tunnel + Vercel로 배포하고 MOSFET UI 추가 및 상용부품 DB 확장을 완료한 세션 기록.

---

## 1. 배포 아키텍처 (구축 완료)

```
브라우저
  └─→ https://web-alpha-two-66.vercel.app (Vercel)
        └─→ Next.js rewrites (/pro/api/*)
              └─→ Cloudflare Quick Tunnel
                    └─→ 로컬 uvicorn :30005 (pro_server/main.py)
                          └─→ SQLite (screening.db, licenses.db)
```

### 실행 중인 서비스

| 서비스 | 설정 파일 | 설명 |
|-------|---------|------|
| `frwaler-pro.service` | `/etc/systemd/system/frwaler-pro.service` | FastAPI 백엔드 (uvicorn, 127.0.0.1:30005) |
| `frwaler-tunnel.service` | `/etc/systemd/system/frwaler-tunnel.service` | Cloudflare Quick Tunnel (URL 매번 변동) |

둘 다 `Restart=always`. Tunnel은 BE 의존성 제거됨 (BE 재시작해도 터널 URL 유지).

### Vercel 배포

- Project: `memorise8s-projects/web`
- Root directory: `web`
- 프로덕션 alias: `https://web-alpha-two-66.vercel.app`
- 환경변수: `NEXT_PUBLIC_PRO_API_URL` (현재 터널 URL)
- 수동 배포: `cd web && npx vercel --prod --yes`

### Quick Tunnel URL 변경 대응

터널 재시작 시 URL 바뀜. 대응 절차:
```bash
# 새 URL 확인
grep trycloudflare /var/log/frwaler-tunnel.log | tail -1

# Vercel 환경변수 갱신
cd web
npx vercel env rm NEXT_PUBLIC_PRO_API_URL production --yes
echo "https://<NEW_URL>.trycloudflare.com" | npx vercel env add NEXT_PUBLIC_PRO_API_URL production
npx vercel --prod --yes
```

근본 해결: Named Tunnel (Cloudflare 도메인 필요) 또는 Named Tunnel 무료 서브도메인.

---

## 2. 코드 변경 요약

### 백엔드

| 파일 | 변경 |
|------|------|
| `pro_server/routers/license.py` | `POST /pro/api/register` 엔드포인트 제거 |
| `pro_server/services/scorer.py` | `_risk_flags`가 BJT 전용 필드를 `getattr`로 안전 접근 (MOSFET 호환) |
| `pro_server/schemas.py` | `ScreeningReport.parameters`를 `Union[BjtParameters, MosfetParameters]`로 변경. `RegisterRequest/Response` 스키마 제거 |
| `pro_server/routers/screening.py` | `_row_to_report`가 param 키로 BJT/MOSFET 자동 감지. `.dict()` → `.model_dump()` (Pydantic v2) |
| `pro_server/data/heritage_bjt_seed.json` | 상용 BJT 10개 추가 (2N2222A, 2N3904, BC547 등) |
| `pro_server/data/heritage_mosfet_seed.json` | 상용 MOSFET 10개 추가 (IRF540N, 2N7000, BSS138 등) |

### 프론트엔드

| 파일 | 변경 |
|------|------|
| `web/src/lib/api.ts` | `PRO_API_BASE = ""` (rewrite 활용). `screenMosfetByFile/ByMpn` 추가. `registerLicense` 제거 |
| `web/src/app/screening/page.tsx` | BJT/MOSFET 토글, 하드코딩 제거, hydration 오류 수정 |
| `web/src/app/screening/[id]/page.tsx` | MOSFET 파라미터 표시 분기, `params` 접근 방식 수정 (Next.js 14) |
| `web/src/app/screening/compare/page.tsx` | BJT/MOSFET 토글 추가 |
| `web/src/components/screening/FactorTable.tsx` | MOSFET factor tooltips 8개 추가 |
| `web/src/components/screening/DatasheetUpload.tsx` | `partLabel` prop으로 동적 라벨 |
| `web/src/app/register/` | 디렉토리 삭제 |
| `web/src/components/NavBar.tsx` | "키 발급" 링크 제거 |

---

## 3. 데이터베이스 현황

### screening.db

| 테이블 | 건수 | 내용 |
|--------|------|------|
| `heritage_parts` (BJT) | **32개** (우주등급 22 + 상용 10) |
| `heritage_parts` (MOSFET) | **28개** (우주등급 18 + 상용 10) |
| `space_factors` | 20개 (BJT 10 + MOSFET 10) |
| `screening_results` | 15+ (테스트 결과) |
| `screening_feedback` | 0 |

### licenses.db

| 테이블 | 건수 |
|--------|------|
| `licenses` | 1 (`test-key-001`, plan=pro, active=1) |
| `usage_log` | 15+ |

### papers.db

**비어있음**. 크롤러 미실행 상태.

---

## 4. 테스트 MPN

### 우주등급 (pass 예상)
- BJT: `JANSR2N2222AUB`, `JANS2N2222AUBCA`, `JANSR2N2907AUB`
- MOSFET: `JANSR2N7268`, `JANSR2N7270`, `JANSR2N7380`

### 상용 (caution 예상)
- BJT: `2N2222A`, `2N3904`, `BC547B`, `MMBT3904`
- MOSFET: `IRF540N`, `2N7000`, `BSS138`, `IRLZ44N`

### 라이선스 키
```
test-key-001
```

### 관리자 비밀번호
```
admin1234
```

---

## 5. 남은 과제 (우선순위)

### HIGH
- [ ] **Linear Regulator 지원 추가** — LM317/NOPB 같은 일반 IC 분석 불가 (BJT/MOSFET 전용)
- [ ] **Op-Amp 지원 추가** — LM358 등
- [ ] **터널 URL 고정** — Named Tunnel + 도메인

### MEDIUM
- [ ] 벤더별 통계 대시보드 (기존 데이터 즉시 활용 가능)
- [ ] 크롤러 실제 실행 → papers.db 채우기
- [ ] papers × heritage 교차 분석 (TID 논문 자동 링크 등)

### LOW
- [ ] `/pro/*` 페이지 스타일 통일 (영어 → 한국어, Tailwind → CSS-in-JS)
- [ ] `api.ts`의 `/api/pro/status`, `/api/pro/usage` 경로 점검 (30004 vs 30005)

---

## 6. 주요 파일 위치

| 경로 | 설명 |
|------|------|
| `/etc/systemd/system/frwaler-pro.service` | BE 서비스 |
| `/etc/systemd/system/frwaler-tunnel.service` | 터널 서비스 |
| `/var/log/frwaler-tunnel.log` | 터널 로그 (URL 확인) |
| `/home/ruci/repo/frwaler/.env` | `GEMINI_KEY` 환경변수 |
| `/home/ruci/repo/frwaler/data/screening.db` | 핵심 DB |
| `/home/ruci/repo/frwaler/data/licenses.db` | 라이선스 DB |
| `/home/ruci/repo/frwaler/pro_server/data/*.json` | Factor/Heritage seed |
| `/home/ruci/.claude/plans/glimmering-waddling-sparkle.md` | 이전 planning 세션 기록 |

---

## 7. 운영 명령어

### 서비스 제어
```bash
sudo systemctl restart frwaler-pro       # BE 재시작
sudo systemctl restart frwaler-tunnel    # 터널 재시작
journalctl -u frwaler-pro -n 30          # BE 로그
```

### Heritage 추가
```bash
# 1. 시드 JSON 수동 편집
# 2. Python으로 INSERT OR IGNORE
/home/ruci/repo/frwaler/.venv/bin/python -c "..."
```

### 패키지 설치
```bash
cd /home/ruci/repo/frwaler
uv pip install <package>
sudo systemctl restart frwaler-pro
```

### 배포
```bash
cd /home/ruci/repo/frwaler/web
npx vercel --prod --yes
```

---

## 8. API 엔드포인트 (pro_server)

| Method | Path | 용도 |
|--------|------|------|
| GET | `/pro/api/health` | Liveness |
| POST | `/pro/api/screen-bjt` | BJT 스크리닝 (file 또는 mpn FormData) |
| POST | `/pro/api/screen-mosfet` | MOSFET 스크리닝 |
| GET | `/pro/api/screen-bjt/{id}` | 리포트 조회 (BJT/MOSFET 공용) |
| GET | `/pro/api/factors?part_type=bjt` | Factor 목록 |
| POST | `/pro/api/feedback` | 피드백 제출 |
| POST | `/pro/api/verify-key` | 라이선스 검증 |
| GET | `/pro/api/usage` | 사용량 |
| GET | `/pro/api/admin/pending` | 관리자: 대기 라이선스 |
| POST | `/pro/api/admin/approve/{key}` | 관리자: 승인 |
| POST | `/pro/api/signup`, `/login`, `/me` | 사용자 계정 (UI 미연동) |

# 납품 준비 상태 · 2026-08-18

전체 점검표(근거 포함): https://claude.ai/code/artifact/388cffff-bcf9-485f-aa88-111ddb0a8e08

목표는 **804대 크롤러를 브라우저만으로 운영**하는 것이다. 수집은 납품처가 새로
시작한다(기존 PDF blob은 이관 대상 아님). 콘솔은 현재 대량 수집을 시작·중지할 수
있으며 실사이트 26대로 확인했다.

---

## 다음에 할 일 (이 순서)

### 1. C1 — 전체 스택 콜드스타트 리허설 · **납품 전 필수**

compose 6개 서비스를 빈 상태에서 함께 띄운 적이 한 번도 없다. 조각만 검증했다:
빈 DB에 `migrate` → `sites` 804행 → `POST /jobs` 200 (격리 PostgreSQL).

미검증인 것:

- 신규 설치 FE가 `BE_URL=http://be:3001` 로 **컨테이너 네트워크를 통해** BE에 붙는 경로
  (지금까지의 FE 확인은 전부 호스트 `127.0.0.1:8081` 로만 했다)
- `postgres → postgres-password-guard → migrate → be → fe → worker` 기동 순서
- `.env` 요구값과 비밀번호 가드의 실제 동작

**운영 스택과 반드시 격리할 것.** `docker compose -p <다른이름>` 과 별도 포트를
쓰면 기존 컨테이너·볼륨(`libertree-delivery_pgdata`, 문서 536,056건)에 닿지 않는다.
여기서 막히면 아래 항목은 모두 무의미하다.

### 2. B1 — 큐 진행 상황 표시 · **납품 전 권장**

작업 목록이 최근 20건만 보여주고 총 대기 건수도 남은 시간도 없다.
실측 **22초/사이트** → 804대 전체 수집은 약 **5시간**. 그동안 화면은 "대기" 20줄만
보여준다. 납품처의 첫 행동이 십중팔구 "전체 수집"이고, 가장 그럴듯한 결말은
"고장났나?" 하고 취소 후 재시도 — 몇 시간을 버리고 외부 사이트를 다시 때린다.

BE 쪽 변경이 필요하다. 스키마 변경은 없다.

```
GET /jobs 응답 키:  jobs, limit, offset     ← total 없음
limit 상한:        200                     ← 804건을 셀 수 없음
필요한 쿼리:        SELECT status, count(*) FROM crawl_jobs GROUP BY status
```

### 3. B2 — 예약 삭제 · **작음**

BE에 `GET`·`PUT` 만 있다. 잘못 만든 예약은 중지만 되고 목록에 영구히 남는다.
`crawl_schedules` 를 참조하는 외래키가 없어 행 삭제는 안전하다(확인함).
`DELETE /schedules/{site_id}` + FE 버튼. 스키마 변경 없음.

### 4. A3 — 조회 API 인증 전제 확정

`@app.get` 17개 중 `require_operator` 가 붙은 것은 **0개**. 쓰기 9개만 토큰을 요구한다.
지금은 포트가 `127.0.0.1` 에만 묶여 가려져 있을 뿐이고, 납품처가 BE를 노출하면
문서 전체가 열람 가능해진다. 프록시 뒤에 두는 전제라면 **런북에 명시**하면 끝난다.
코드 변경 없이 문서로 끝날 수도 있는 항목이다.

### 5. A1 · A2 — 실패를 성공으로 보고하는 문제 · **납품 후로 미뤄도 됨**

- **A1**: 크롤러가 모든 요청을 거부당해도 예외 없이 끝나면 `done`·`error=null` 로 기록된다.
  실측: 작업 #9 `justice-govt-nz-publications` — AWS WAF 캡차로 51회 전부 거부,
  3분 53초, 기록은 `done / saved_count=0 / error=null`.
- **A2**: 크롤러 30개가 `LIBERTREE_MAX_WALL_S`(기본 25분) 초과 시 남은 섹션을
  건너뛰고 정상 반환한다. 대형 사이트에서 부분 수집인데 완료로 보인다.

둘 다 워커가 "예외 없이 끝남"을 "성공"으로 취급하는 데서 온다. 크롤러 반환 계약까지
손대야 해서 크다. **크롤러 원문 로그를 저장하고 있으므로 진단은 가능하다** — 목록에서
한눈에 안 보일 뿐이다.

자동 판정("0건이면 실패")은 넣지 않았다. 정상적으로 0건인 증분 수집을 오분류한다.

### 납품 조건이 아닌 것

- **jsdom / 클라이언트 컴포넌트 테스트 9개** — 납품처가 겪는 것이 달라지지 않는다.
  앞으로의 변경을 안전하게 만드는 장치다. (오늘 취소 라벨 결함을 테스트가 아니라
  실전 실행으로 잡은 것은 사실이다.)
- **일괄 예약** — 20~30개면 브라우저로 충분하다. 804대일 때만 걸린다.
- **수집률** — `docs/proposals/2026-08-16-crawl-coverage.md` 로 이관됨.

---

## 환경

```
운영 스택   libertree-delivery-{postgres,be,worker}-1     (compose: delivery/docker-compose.yml)
BE          127.0.0.1:8081  →  컨테이너 3001
FE(dev)     127.0.0.1:3002  ·  delivery/fe/.env.local 에 BE_URL·DELIVERY_API_TOKEN
            (gitignore .gitignore:71 — 없으면 BE_URL 이 8080으로 떨어져 조용히 빈 화면이 된다)
브랜치      delivery-mvp-audit-20260812   (origin 에 아직 push 안 됨 · 자격증명 없음)
```

테스트:

```
FE       cd delivery/fe && npm test && npm run typecheck && npm run lint      # 253
워커/BE  격리 postgres 컨테이너를 띄우고 TEST_PG_DSN 지정 후                    # 66
         docker run --rm --network container:<pg> -v <repo>:/app -w /app \
           -e TEST_PG_DSN=... libertree-delivery-worker python -m unittest tests.test_worker_run ...
```

## 지켜야 할 것

- 운영 DB에 절대 테스트를 겨누지 않는다. 테스트는 `DROP SCHEMA` 를 한다.
  임시 postgres 컨테이너(`--tmpfs`, `--rm`)를 띄워서 쓴다.
- `git add -A` / `git add .` 금지. 다른 세션이 같은 브랜치에 파이썬을 커밋한다.
- `delivery/fe/next-env.d.ts` 와 `tsconfig.json` 은 스테이징하지 않는다.
  Next가 dev/build 에 따라 계속 고쳐 쓴다.
- 워커는 살아 있다. 실사이트 site_id로 작업을 넣으면 **실제로 크롤한다.**
  검증에는 존재하지 않는 site_id를 쓰고, 전후로 `crawl_jobs` 수를 기록한다.

## 이미 확인된 것 (재검토 불필요)

빈 설치에서 수집 시작(804 seed · POST /jobs 200) · 조건별 일괄 실행(독일 26대,
등록 1초 미만) · 내 작업만 골라 중지(15건 지정 → 정확히 15건) · 소요 시간 측정
(8.8s~45.1s, 뭉개짐 0건) · 크롤러 원문 로그(11개 작업 55줄) · 납품 검증과 이 설치
결과 병기 · 번역이 크롤을 굶기지 않음 · 실패 크롤러를 방치 큐에서 분리(162→150).

## 데이터

| 항목 | 수치 | 메모 |
|---|---|---|
| 등록 수집기 | 804 | 신규 설치 시 `migrate` 가 `sites` 에 자동 등록 |
| 감사 정상·실패 | 772 · 32 | 2026-08-06 다른 네트워크 기준, 갱신되지 않음 |
| 여기서 수집 확인 | 8 | 이 설치에서 실제로 문서를 저장한 사이트 |
| 수집기 없는 사이트 | 1 | `scienceon-api` · 문서 10,447건 · 실행 불가 |
| 작업 이력 | 영구 | 의도적 — 검증 집계가 이 이력에서 계산됨 |
| 크롤러 로그 | 14일 | 작업당 최대 40줄 |

**PDF blob은 접었다.** DB는 30만 건 확보를 기록하지만 볼륨은 비어 있고 문서 상세의
"PDF 열기"가 전부 404다. 재수집 전제에서는 문제가 아니지만, **지금 이 DB를 함께
넘긴다면 되살아난다.** 연결 절차는 `docs/DELIVERY_BLOB_MOUNT.md`.

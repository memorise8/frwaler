# 인계 — 납품 진행 상태 · 2026-08-19

다음 세션은 이 문서 하나로 이어서 진행할 수 있다. 이전 인계(`HANDOFF_20260818_SCALE.md`)의
규모 문제는 **해소되었고**, 지금은 **납품 데이터 전송이 진행 중**이다.

---

## 0. 지금 상태 한눈에

```
브랜치     delivery-mvp-audit-20260812 (원격과 동일, 작업트리 clean)
전송       libertree-transfer 도커 컨테이너가 1.7TB blob 을 고객 서버로 전송 중
           (세션 독립·자동 재시도·이어받기. 2026-08-19 12:36 시작, 예상 2.5~3일)
확인       docker logs libertree-transfer | tail        ← 진행/재시도
           로그에 "TRANSFER-COMPLETE" 가 찍히면 완료
고객서버   211.170.66.98:5522, 계정 wise (Windows + OpenSSH + cwRsync)
           비밀번호는 /mnt/raid/ruci_workspace/frwaler-delivery-out/remote.txt (git 밖)
대상경로   E:\wise\{bundle, dump, blob}
```

전송이 죽어 있으면(컨테이너 없음/Exited): `sh /mnt/raid/ruci_workspace/frwaler-delivery-out/transfer.sh`
를 같은 마운트로 다시 띄우면 이어받는다 — 실행 명령은 §5 참고.

---

## 1. 이번 납품에서 개발·완료된 것 (전부 리뷰·실물검증 통과, 푸시됨)

### A2 — 부분 수집 표시 (완료)
- `crawl_jobs.truncated` + 워커가 **경과 시간**으로 잘림 판정(크롤러 출력 문자열 파싱 아님)
- FE 「부분 수집」 배지, `LIBERTREE_MAX_WALL_S` 를 compose/.env.example/런북에 노출

### 용량 교정 (완료)
- `capacity_final.csv` 가 4배 과소평가였음을 발견 → `scripts/audit/correct_capacity.py` 로
  전 감사 CSV 병합(재현 가능·출처 추적). **보정 총계 24,330,001건**, 10만+ 사이트 **25개**
- 산출물 `capacity_corrected.csv` (시드가 쓰는 사본: `delivery/vendor/capacity_corrected.csv`)
- 실 blob 은 1.67TB (pdf 303,739 + txt 288,140) — DB pdf_size_bytes 합계와 일치 확인

### 재개(cursor)/백필 — 대형 사이트 완주 기능 (완료, 이 납품의 최대 개발)
- `crawl_site_progress`(cursor JSONB·items_done·total_estimate·completed_at)
- `mode="backfill"`: 워커가 커서 주입→잘리면 저장+**큐 맨 뒤 자동 재큐잉**→크롤러의
  **명시적 소진 신호**(`_mark_exhausted`)가 있을 때만 완주. 시간 추론 완주 금지(스펙 §2 보정)
- `CrawlControl(BaseException)` 계열 — 크롤러 733/800 의 `except Exception` 이 못 삼키게.
  (부수 효과: 기존에 삼켜지던 취소도 완치)
- 대형 크롤러 25개 패치: 커서 읽기/보고 + run당 페이지 예산(절대 캡 아님) + 소진 신호
  + 실패≠빈페이지 분리. HAL 6개는 `sort=docid asc` 통일(oldest_first)
- 증분: newest_first 12개는 연속 50건 기보유 시 `CrawlUpToDate` 조기 종료
- 시드 `delivery/scripts/seed_backfill_estimates.py`(25사이트 total_estimate), `GET /progress`,
  FE `/backfill` 화면
- 검증 자산: `tests/test_crawler_cursor_{contract,conformance}.py` — 특히
  **센티널 행위 테스트**(전 네트워크 진입점을 BaseException 으로 차단하고 거대 커서
  재개가 fetch 를 시도하는지) 와 **소진 신호 행위 핀**(실패값/빈값 스텁)
- 계획·스펙: `docs/superpowers/{plans,specs}/2026-08-18-resume-cursor*.md`

### 납품 패키징 (완료)
- `.gitattributes export-ignore` 로 번들 스코프 확정: **1,547파일** — crawler/delivery/
  tests/ + 고객용 docs 30개만. 레거시(libertree-app)·무관 프로젝트(finolaw·ecip·api)·
  내부 문서 41개·`scripts/` 전부·내부 IP/호스트명 문서 제외
- **모든 패턴은 / 루트 고정** — 미고정이 두 번 사고 냄(delivery/data/site-taxonomy.json,
  delivery/fe/src/app/api/ 가 빠져 번들 콜드스타트에서 BE 크래시)
- 빌드가 의존하는 레거시 파일은 `delivery/vendor/` 로 이관(원본과 동기화 핀:
  `tests/test_delivery_vendor.py`)
- 실물 검증 2종 통과: **번들 콜드스타트**(추출→migrate→up→API/FE/프록시/시드→철거)와
  **정보 유출 스캔**(비밀값 0·인프라 식별자 0)

### 설명서 (완료)
- `docs/deliverables/사용설명서_20260818.html` + **https://libertree-manual.vercel.app**
  (비밀번호 `libertree` — 본문을 AES 암호화, 소스에 평문 없음. Vercel 계정 memorise8)

---

## 2. 전송 구성 (진행 중)

| 대상 | 내용 | 상태 |
|---|---|---|
| `E:\wise\bundle\` | `libertree-delivery-c513b2c35c8f.tar.gz`(3.9MB) + sha256 매니페스트 2종 | ✅ 완료 |
| `E:\wise\dump\` | `libertree-20260819.dump` 743MB — documents 536,056행, **운영 이력 8테이블은 스키마만**(crawl_jobs·crawl_job_logs·crawl_schedules·translation_{jobs,job_attempts,batches,worker_heartbeats,system_observations}) | ✅ 완료 |
| `E:\wise\blob\` | 주 루트 `/mnt/raid/ruci_workspace/frwaler_job/libertree/`(1.67TB) → 후속으로 보조 루트 `frwaler_job/data/0000`(주에 없는 4,035파일, `--ignore-existing`) | ⏳ 진행 |

- blob 의 진실: DB 는 pdf 30.3만 건(1,667GB)을 가리키고 실파일도 30.4만 개로 일치.
  **주의: 두 blob 루트가 존재** — 위 두 경로가 전부이고 delivery 스택의 도커 볼륨은 비어 있음
- Windows 대상이라 rsync 는 `-rt --no-perms --no-owner --no-group` (ACL 로 -a 불가)
- 전송 완료 후 스크립트가 자동으로 dry-run 검증(잔여 0건) 출력

### 전송 완료 후 고객이 할 일(설명서 2·3절에 있음)
bundle 풀기 → `.env` 작성(토큰 생성) → bootstrap migrate --build → pg_restore →
blob 을 `BLOB_HOST_PATH` 로 마운트(`docker-compose.blob.yml`) → 문서 수 536,056 대조

---

## 3. 남은 것 (의도적 연기 포함)

1. **전송 완료 확인 + 원격 검증** — TRANSFER-COMPLETE 후 파일 수/무결성 확인, 고객 안내
2. **doaj 첫 backfill** — 고객(또는 우리) 환경에서 시작하는 것이 실전 검증.
   stalled 경고가 뜨면 그게 A1 신호이기도 함
3. **A1(차단 감지)** — 의도적으로 납품 후. 운영 로그 쌓인 뒤 실제 차단 사이트만 수리
4. **single_source 재검증 후보** — ots-at-pressemappe(1.56M, 코퍼스 3위)·mof-go-kr-doc:
   측정 출처가 1개뿐. 백필이 곧 실측이 된다
5. 파킹된 소소한 것들은 각 계획의 최종 리뷰에 기록됨(치명 없음)

---

## 4. 지켜야 할 것 (변함없음)

- **워커는 살아 있다** — 실존 site_id 로 작업/스케줄을 넣으면 실제 크롤. 검증은 가짜
  site_id·주입 크롤러·일회용 `--tmpfs --rm` postgres 만
- **`git add -A`/`git add .` 금지** (동시 세션), `delivery/fe/tsconfig.json`·`next-env.d.ts` 스테이징 금지
- **운영 스택(`libertree-delivery`) `down -v` 금지** — 문서 536,056건. 8080 은 남의 프로젝트
- 비밀값(POSTGRES_PASSWORD·DELIVERY_API_TOKEN·GITHUB_TOKEN·고객 서버 비밀번호) 출력 금지.
  푸시는 `delivery/.env` 의 GITHUB_TOKEN 을 askpass 로(따옴표 벗겨서), 흔적 안 남게
- 번들 관련 파일을 만들면 **`.gitattributes` 루트 고정 여부**부터 의심할 것

---

## 5. 자주 쓰는 명령

```bash
# 전송 상태
docker logs libertree-transfer 2>&1 | tail -5
docker logs libertree-transfer 2>&1 | grep -c 재시도

# 전송 재기동(죽었을 때 — 이어받음)
docker run -d --name libertree-transfer --restart on-failure:5 \
  -e SSHPASS="$(cat /mnt/raid/ruci_workspace/frwaler-delivery-out/remote.txt | sed -n 's/^pw=//p')" \
  -v /mnt/raid/ruci_workspace/frwaler-delivery-out:/out:ro \
  -v /mnt/raid/ruci_workspace/frwaler_job/libertree:/src/main:ro \
  -v /mnt/raid/ruci_workspace/frwaler_job/data/0000:/src/extra/0000:ro \
  alpine:latest sh /out/transfer.sh

# 번들 생성 / 파이썬·FE 테스트
DELIVERY_RELEASE_CONFIRM=BUILD_COMMITTED_SOURCE_BUNDLE RELEASE_DIR=<dir> sh delivery/scripts/build_release_bundle.sh
# (파이썬: rc-testpg 일회용 컨테이너 패턴 — HANDOFF_20260818_SCALE.md §테스트 명령 참조)
cd delivery/fe && npm test && npm run typecheck && npm run lint && npm run build
```

기준선: FE 291 (29파일) · 파이썬 전체 ~280 (모듈 15+) · conformance 는 컨테이너에서 실행
(호스트에 bs4 없음).

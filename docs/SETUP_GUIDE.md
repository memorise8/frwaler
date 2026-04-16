# BJT 업스크리닝 서비스 — Dev 서버 셋업 가이드

> **TL;DR**: `scripts/init_prod.sh` 한 번 실행으로 venv 생성 → deps 설치 → DB 시드 → 라이선스 키 생성 → 테스트까지 자동화됩니다.
> 수동으로 이해하며 진행하려면 아래 순서대로.

## 0. 사전 준비

필요 도구:
- Python 3.12+
- Node.js 18+
- `uv` (Python 패키지 매니저, pip 대체)
- git

```bash
# uv 설치
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # 또는 ~/.zshrc

# 버전 확인
python3 --version   # 3.12+
node --version      # v18+
uv --version
```

프로젝트 복사 (택 1):
```bash
# A) 운영 서버에서 scp로 복사
scp -r user@prod-server:/path/to/frwaler /home/user/frwaler

# B) git clone
git clone https://github.com/memorise8/frwaler.git
cd frwaler
git checkout hackerton
```

---

## 1. Python 환경 설정

```bash
cd /path/to/frwaler

# 가상환경 생성 + 의존성 설치
uv venv --python 3.12
uv pip install -r requirements.txt

# 설치 확인
.venv/bin/python -c "import fastapi, openai, pdfplumber; print('OK')"
```

---

## 2. 환경변수 설정 (.env)

프로젝트 루트에 `.env` 파일 생성. **LLM 키는 Gemini 우선, OpenAI는 선택적 폴백**입니다.

```bash
cat > .env << 'EOF'
# LLM 키 — 둘 중 최소 1개 필수 (Gemini 우선, 실패 시 OpenAI 폴백)
GEMINI_KEY=AQ.실제-키...
# PRO_OPENAI_API_KEY=sk-proj-...   # 폴백용, 선택

PRO_SCREENING_DB_PATH=./data/screening.db
PRO_LICENSE_DB_PATH=./data/licenses.db

# 프로덕션 CORS 제한 (콤마 구분, 비워두면 전체 허용 — dev에선 비워둬도 됨)
# PRO_ALLOWED_ORIGINS=https://your-app.vercel.app
EOF

chmod 600 .env
```

> `.env`는 `.gitignore`에 등록되어 있습니다. git에 절대 커밋하지 마세요.

### Gemini API 키 발급 (권장 — 무료 500건/일)

1. https://aistudio.google.com/apikey 접속 (Google 계정 로그인)
2. "Create API key" 클릭
3. 키 복사 → `.env`의 `GEMINI_KEY=` 뒤에 붙여넣기
4. 무료 티어: Gemini 2.5 Flash 기준 **500건/일**, 10 RPM
5. 한도 및 사용량: https://ai.google.dev/gemini-api/docs/rate-limits

### OpenAI API 키 발급 (선택 — Gemini 한도 초과 시 폴백)

1. https://platform.openai.com/api-keys 접속
2. "+ Create new secret key" 클릭, 이름: `frwaler-bjt-dev`
3. 키 복사 → `.env`의 `PRO_OPENAI_API_KEY=` 뒤에 붙여넣기
4. 비용 한도 설정 권장: https://platform.openai.com/settings/organization/limits ($10~$50/월)
5. gpt-4o-mini 요청당 ~$0.001 (1원 수준)

---

## 3. 백엔드 서버 실행

```bash
cd /path/to/frwaler

# .env 로드 (택 1)
export $(grep -v '^#' .env | xargs)     # 방법 A
# source .env                            # 방법 B (일부 shell)

# 데이터 디렉토리 생성 (최초 1회)
mkdir -p data

# 서버 시작 (DB 마이그레이션 + 시드 자동 적용)
.venv/bin/python -m uvicorn pro_server.main:app --host 0.0.0.0 --port 30005 --reload
```

서버 기동 확인:
```bash
curl http://localhost:30005/pro/api/health
# 기대 응답: {"status":"ok","mode":"pro"}
```

---

## 4. 라이선스 키 생성

서버 API를 호출하려면 라이선스 키가 필요합니다. 최초 1회 생성:

```bash
export $(grep -v '^#' .env | xargs)

.venv/bin/python -c "
import sqlite3, os
db = os.environ.get('PRO_LICENSE_DB_PATH', './data/licenses.db')
conn = sqlite3.connect(db)
conn.execute(\"INSERT OR IGNORE INTO licenses (key, owner, plan, active) VALUES ('DEV-KEY-1', 'dev-user', 'pro', 1)\")
conn.commit()
conn.close()
print('License key created: DEV-KEY-1')
"
```

---

## 5. 백엔드 동작 확인 (스모크 테스트)

```bash
# 헬스 체크
curl http://localhost:30005/pro/api/health

# Factor 목록 조회 (OpenAI 키 불필요)
curl -H "X-License-Key: DEV-KEY-1" \
     "http://localhost:30005/pro/api/factors?part_type=bjt"

# Heritage MPN 조회 (DB에 이미 있는 부품 — OpenAI 키 불필요)
curl -H "X-License-Key: DEV-KEY-1" \
     -X POST \
     -d "mpn=JANSR2N2222AUB" \
     http://localhost:30005/pro/api/screen-bjt

# PDF 데이터시트 업로드 (OpenAI 키 필요)
curl -H "X-License-Key: DEV-KEY-1" \
     -F "file=@/path/to/bjt-datasheet.pdf" \
     http://localhost:30005/pro/api/screen-bjt
```

---

## 6. 프론트엔드 설정 + 실행

```bash
cd /path/to/frwaler/web
npm install

# 개발 모드 실행 (next.config.js에 localhost:30005 프록시 자동 설정됨)
npm run dev
```

브라우저에서 접속: http://localhost:3001/screening

프론트가 백엔드를 찾지 못할 경우:
```bash
# web/.env.local 파일 생성
cat > web/.env.local << 'EOF'
NEXT_PUBLIC_API_URL=http://localhost:30005
NEXT_PUBLIC_PRO_API_URL=http://localhost:30005
EOF
```

---

## 7. 테스트 실행

```bash
cd /path/to/frwaler
export $(grep -v '^#' .env | xargs)

.venv/bin/python -m pytest tests/ -v
# 기대: 57 passed
```

---

## 8. Vercel 배포 (프론트엔드만)

### 8-1. Vercel CLI 설치 및 로그인
```bash
npm i -g vercel
vercel login
```

### 8-2. 프로젝트 링크
```bash
cd web
vercel link
# 기존 프로젝트 선택 또는 새로 생성
```

### 8-3. 환경변수 설정 (Vercel 대시보드)
Vercel 대시보드 → 프로젝트 → Settings → Environment Variables:
- `NEXT_PUBLIC_API_URL` = 백엔드 외부 URL (아래 8-5 참고)
- `NEXT_PUBLIC_PRO_API_URL` = 동일

⚠️ `PRO_OPENAI_API_KEY`는 절대 Vercel에 넣지 마세요. 프론트엔드에 불필요하고 브라우저에 노출됩니다.

### 8-4. 배포
```bash
cd web
vercel          # preview 배포
vercel --prod   # production 배포
```

### 8-5. 백엔드 외부 노출 (택 1)

**A) Cloudflare Tunnel (무료, 간편)**
```bash
# cloudflared 설치 후
cloudflared tunnel --url http://localhost:30005
# 발급된 URL을 Vercel 환경변수 NEXT_PUBLIC_API_URL에 설정
```
단점: 터미널 종료 시 같이 종료됨.

**B) Railway / Fly.io (안정적, 유료)**
```bash
# Railway 예시
railway login && railway init && railway up
# 발급된 고정 URL을 Vercel 환경변수에 설정
```

**C) 같은 서버에서 포트 개방**
- 방화벽에서 30005 포트 개방
- `NEXT_PUBLIC_API_URL=http://서버IP:30005`
- HTTPS 적용 시: nginx reverse proxy + Let's Encrypt 권장

---

## 9. 체크리스트

### 필수 (로컬 동작)
- [ ] Python 3.12 + uv 설치
- [ ] `uv pip install -r requirements.txt`
- [ ] `.env` 파일에 `PRO_OPENAI_API_KEY` 설정
- [ ] `mkdir -p data` 후 uvicorn 서버 시작
- [ ] 라이선스 키 1개 이상 생성
- [ ] `curl` 스모크 테스트 통과
- [ ] Node.js 18+ 설치 + `npm install` (web/)
- [ ] `npm run dev` 접속 확인 (port 3001)

### 선택 (배포/운영)
- [ ] pytest 57/57 통과 확인
- [ ] Vercel 배포 (프론트)
- [ ] 백엔드 외부 URL 확보 (tunnel / Railway / 포트 개방)
- [ ] Vercel 환경변수 설정
- [ ] OpenAI 월 한도 설정 ($10~$50 권장)
- [ ] `.env` 파일 권한 `chmod 600 .env`

### 절대 하지 말 것
- `.env`를 git commit/push
- Vercel에 `PRO_OPENAI_API_KEY` 추가
- OpenAI 키를 소스 코드에 하드코딩
- `NEXT_PUBLIC_OPENAI_API_KEY` 사용 (브라우저에 노출됨)

---

## 10. 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `No LLM provider configured` | `.env` 미로드 또는 키 미입력 | `export $(grep -v '^#' .env \| xargs)` 후 서버 재시작. GEMINI_KEY 또는 PRO_OPENAI_API_KEY 중 최소 1개 설정 |
| Gemini 429 quota exceeded | Gemini 무료 한도 초과 (500건/일) | OpenAI 키를 `.env`에 추가하면 자동 폴백 |
| `401 License key required` | 라이선스 키 미생성 또는 헤더 누락 | 4번 참고하여 키 생성, `-H "X-License-Key: DEV-KEY-1"` 확인 |
| `401 Invalid license key` | 키가 DB에 없음 | 4번 스크립트 재실행 |
| `429 Daily rate limit exceeded` | 일일 100건 초과 | `settings.py`의 `max_requests_per_day` 값 조정 |
| 프론트에서 "Network Error" | 백엔드 미실행 또는 포트 불일치 | `curl localhost:30005/pro/api/health` 확인 |
| `npm run dev` 접속 안 됨 | 포트 충돌 또는 빌드 오류 | `node -v` → 18+, `rm -rf web/node_modules && npm install` |
| `npm run build` 실패 | 타입 오류 또는 의존성 문제 | `node -v` → 18+, `rm -rf web/.next web/node_modules && npm install` |
| pytest import 에러 | venv 미활성화 | `.venv/bin/python -m pytest` 명시적 경로 사용 |
| `data/` 디렉토리 없음 오류 | 최초 실행 시 디렉토리 부재 | `mkdir -p data` 실행 후 서버 재시작 |

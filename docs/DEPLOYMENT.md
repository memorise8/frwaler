# 배포 가이드

BE는 Cloudflare Tunnel(Quick Tunnel)로 외부 노출, FE는 Vercel에 배포하는 구성입니다.

## 아키텍처

```
[브라우저]
  ↓
[Vercel FE (Next.js)]              https://web-alpha-two-66.vercel.app
  ↓ next.config.js rewrites: /pro/api/* → ${NEXT_PUBLIC_PRO_API_URL}/pro/api/*
  ↓
[Cloudflare Quick Tunnel]          https://<random>.trycloudflare.com
  ↓
[로컬 서버: pro_server]            127.0.0.1:30005
  ↓
[SQLite: licenses.db, screening.db]
```

현재는 `pro_server` (30005)만 배포된 상태. `api` (30004)는 미배포.

---

## 1. 서버 (로컬 머신) — 최초 1회 세팅

### systemd 서비스 2개 등록 완료

| 서비스 | 역할 | 파일 |
|--------|------|------|
| `frwaler-pro` | uvicorn으로 pro_server 실행 | `/etc/systemd/system/frwaler-pro.service` |
| `frwaler-tunnel` | cloudflared Quick Tunnel | `/etc/systemd/system/frwaler-tunnel.service` |

### frwaler-pro.service 내용

```ini
[Unit]
Description=Frwaler PRO API (port 30005)
After=network.target

[Service]
Type=simple
User=ruci
WorkingDirectory=/home/ruci/repo/frwaler
EnvironmentFile=/home/ruci/repo/frwaler/.env
ExecStart=/home/ruci/repo/frwaler/.venv/bin/uvicorn pro_server.main:app --host 127.0.0.1 --port 30005
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### frwaler-tunnel.service 내용

```ini
[Unit]
Description=Frwaler Cloudflare Quick Tunnel (30005)
After=network.target frwaler-pro.service
Requires=frwaler-pro.service

[Service]
Type=simple
User=ruci
ExecStart=/home/ruci/.local/bin/cloudflared tunnel --url http://127.0.0.1:30005
Restart=always
RestartSec=5
StandardOutput=append:/var/log/frwaler-tunnel.log
StandardError=append:/var/log/frwaler-tunnel.log

[Install]
WantedBy=multi-user.target
```

### 환경

- Python venv: `/home/ruci/repo/frwaler/.venv` (uv로 관리 → `uv pip install ...`)
- `.env` 파일: `/home/ruci/repo/frwaler/.env` (GEMINI_KEY 등)
- 관리자 비밀번호: `admin1234` (`pro_server/settings.py` 기본값)

---

## 2. Vercel (FE)

- 프로젝트: `memorise8s-projects/web`
- 프로덕션 alias: https://web-alpha-two-66.vercel.app
- Root Directory: `web`
- 환경변수: `NEXT_PUBLIC_PRO_API_URL` = 현재 터널 URL

**주의**: Git 자동 배포 연결 안 되어 있음. 수동 배포 필요 (`npx vercel --prod --yes`).

---

## 3. 새 코드 배포 절차 (일반 케이스)

```bash
cd /home/ruci/repo/frwaler

# 1) 최신 코드 pull
git pull

# 2) Python 새 의존성 있으면 설치
uv pip install -r requirements.txt

# 3) BE 재시작 (sudo 필요)
sudo systemctl restart frwaler-pro

# 4) BE 정상 확인
curl http://127.0.0.1:30005/pro/api/health

# 5) FE 배포
cd web
npx vercel --prod --yes
```

---

## 4. 터널 URL이 바뀌었을 때

Quick Tunnel은 `cloudflared` 프로세스 재시작 시마다 URL이 바뀝니다.

### 현재 URL 확인

```bash
grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /var/log/frwaler-tunnel.log | tail -1
```

### Vercel 환경변수 교체 + 재배포

```bash
cd /home/ruci/repo/frwaler/web

# 기존 env 제거
npx vercel env rm NEXT_PUBLIC_PRO_API_URL production --yes

# 새 URL 등록
npx vercel env add NEXT_PUBLIC_PRO_API_URL production <<< "https://<NEW-URL>.trycloudflare.com"

# 프로덕션 재배포
npx vercel --prod --yes
```

---

## 5. 디버깅

### BE 로그

```bash
journalctl -u frwaler-pro --no-pager -n 50
```

### 터널 로그

```bash
tail -50 /var/log/frwaler-tunnel.log
```

### 상태 확인

```bash
systemctl is-active frwaler-pro
systemctl is-active frwaler-tunnel
curl http://127.0.0.1:30005/pro/api/health
curl https://<TUNNEL-URL>/pro/api/health
```

### 서비스 재시작

```bash
sudo systemctl restart frwaler-pro       # BE만
sudo systemctl restart frwaler-tunnel    # 터널만 (URL 바뀜!)
```

---

## 6. 흔한 함정

- **`pip`가 없다** → venv가 uv 기반이라 pip이 없음. `uv pip install <pkg>` 사용
- **BE가 import 실패로 시작 안 됨** → `journalctl -u frwaler-pro` 로 `ModuleNotFoundError` 확인 후 `uv pip install`
- **FE에서 API 호출 실패** → Vercel `NEXT_PUBLIC_PRO_API_URL` 이 현재 터널 URL과 일치하는지 확인
- **터널 URL이 바뀌었다** → 4번 섹션 참조
- **CORS 에러** → pro_server는 `*.vercel.app` preview 도메인 자동 허용 (`pro_server/main.py:15`). 커스텀 도메인 쓸 거면 `PRO_ALLOWED_ORIGINS` 환경변수 설정

---

## 7. TODO (선택)

- [ ] Vercel Git Integration 연결 → push 시 자동 배포
- [ ] api 서버(30004) 별도 터널 추가 → 크롤링/제품검색 기능 활성화
- [ ] Quick Tunnel → Named Tunnel 마이그레이션 (URL 고정, 도메인 필요)

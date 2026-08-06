#!/usr/bin/env bash
# 독립 실행용 크롤러 공유 패키지(crawlers-share/) 재생성 + tarball.
#
# 커밋되는 것: crawlers-share/{run.py,list_sites.py,README.md,requirements.txt,.env.example}
# 재생성되는 것(gitignore): crawlers-share/crawler/ (크롤러 런타임 복사본) + crawlers-share.tar.gz
#
# 제외: 비밀키(.env), 파이썬 캐시, LLM/요약/자동생성기 모듈(수집에는 불필요, 내부 전용).
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=crawlers-share

echo "[build] crawler 런타임 복사 -> $DEST/crawler"
rm -rf "$DEST/crawler"
rsync -a \
  --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='main.py' \
  --exclude='agent.py' --exclude='agent_tools.py' \
  --exclude='claude_runner.py' --exclude='codex_runner.py' \
  --exclude='smart_finder.py' --exclude='llm_providers.py' \
  --exclude='analyzer.py' --exclude='summarizer.py' \
  --exclude='translation_schema.py' \
  crawler/ "$DEST/crawler/"

# .env.example 재생성 (키 이름만, 값·주석 미복사). crawler/.env 없으면 기존 파일 유지.
if [ -f crawler/.env ]; then
  echo "[build] .env.example 재생성 (키 이름만)"
  python - <<'PY'
import re
src, dst = "crawler/.env", "crawlers-share/.env.example"
keys, seen = [], set()
for line in open(src, encoding="utf-8"):
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if m and m.group(1) not in seen:
        seen.add(m.group(1)); keys.append(m.group(1))
analysis = {"OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER"}
out = [
 "# ------------------------------------------------------------------",
 "# 크롤러 실행용 환경변수 예시",
 "#  - 값을 채운 뒤 이 파일을 .env 로 복사하세요.",
 "#  - 대부분의 크롤러는 키가 필요 없습니다.",
 "#  - 아래 *_KEY 항목은 해당 사이트의 공개 검색 API 키가 필요한 경우에만.",
 "#  - OPENAI/GEMINI/LLM_PROVIDER 는 분석·요약용이며 '수집만' 한다면 불필요합니다.",
 "# ------------------------------------------------------------------",
 "",
]
for k in keys:
    out.append(f"# {k}=        # (선택: 분석/요약용, 수집에는 불필요)" if k in analysis else f"{k}=")
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"  keys: {len(keys)}")
PY
fi

echo "[build] 비밀키 유출 검사"
if grep -rInE "sk-proj-[A-Za-z0-9_-]{20}|sk-[A-Za-z0-9]{40,}|AIzaSy[A-Za-z0-9_-]{30}" "$DEST" 2>/dev/null; then
  echo "[build] ABORT: 비밀키가 패키지에 유출됨" >&2
  exit 1
fi

echo "[build] tarball 생성 -> crawlers-share.tar.gz"
tar -czf crawlers-share.tar.gz "$DEST"

CNT=$(cd "$DEST" && PYTHONPATH=. python -c "from crawler.sites import CRAWLERS; print(len(CRAWLERS))" 2>/dev/null || echo "?")
echo "[build] 완료: 크롤러 $CNT개 | $(du -sh "$DEST" | cut -f1) | tarball $(du -sh crawlers-share.tar.gz | cut -f1)"

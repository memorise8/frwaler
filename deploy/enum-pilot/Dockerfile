# 공통 수집 틀(crawler.enumeration) 시범 실행용 격리 이미지.
# 빌드 컨텍스트는 레포 루트:
#   docker build -f deploy/enum-pilot/Dockerfile -t enum-pilot:<날짜> .
# 브라우저(Playwright 크로미움)는 기본 미설치. 틀이 브라우저 수집기를 거부하므로 시범에는 불필요.
#   --build-arg WITH_BROWSER=1 로만 설치.
FROM python:3.12-slim

ARG WITH_BROWSER=0

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    LIBERTREE_BLOB_ROOT=/data/blob

# delivery/Dockerfile.worker 와 같은 이유: pdftotext(poppler-utils), curl 을 쓰는 수집기가 있다.
RUN apt-get update && apt-get install -y --no-install-recommends curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY delivery/requirements.txt /app/delivery/requirements.txt
RUN pip install --no-cache-dir -r /app/delivery/requirements.txt
COPY delivery/vendor/crawler-runtime-requirements.txt /app/crawler-runtime-requirements.txt
RUN pip install --no-cache-dir -r /app/crawler-runtime-requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_BROWSER" = "1" ]; then python -m playwright install --with-deps chromium; fi

COPY crawler /app/crawler
# 틀이 import 하는 delivery 쪽 최소 모듈 (grep 확인:
#   enumeration/http.py -> delivery.health.{access_challenge,request_gate,source_observer}
#   enumeration/{runner,parsing}.py -> delivery.scripts.discovery_structure
#   위 모듈의 상대 import -> health/{diagnostics,results,scope_inventory}
#   PDF 본문 품질 검사 수집기(academie·kei·krivet) -> delivery.files.text_quality (표준 라이브러리만))
COPY delivery/__init__.py /app/delivery/__init__.py
COPY delivery/health/__init__.py \
     delivery/health/access_challenge.py \
     delivery/health/diagnostics.py \
     delivery/health/request_gate.py \
     delivery/health/results.py \
     delivery/health/scope_inventory.py \
     delivery/health/source_observer.py \
     /app/delivery/health/
COPY delivery/scripts/discovery_structure.py /app/delivery/scripts/discovery_structure.py
COPY delivery/files/__init__.py delivery/files/text_quality.py /app/delivery/files/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin enumpilot \
    && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

# state, db, reports, blob 는 모두 /data 아래. 호스트 디렉터리를 마운트해 사용.
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "crawler.enumeration.run"]

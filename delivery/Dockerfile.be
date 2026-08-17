FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY delivery/requirements.txt /app/delivery/requirements.txt
RUN pip install --no-cache-dir -r /app/delivery/requirements.txt
# 크롤러 런타임 의존성(requests, beautifulsoup 등)도 필요
COPY crawlers-share/requirements.txt /app/crawler-runtime-requirements.txt
RUN pip install --no-cache-dir -r /app/crawler-runtime-requirements.txt
COPY crawler /app/crawler
COPY delivery /app/delivery
# The migration seeds `sites` from the audit catalogue so a new install can
# start crawls from the console; without this file it would seed nothing.
COPY scripts/audit/crawler_status_final.csv /app/scripts/audit/crawler_status_final.csv
EXPOSE 3001
CMD ["uvicorn", "--factory", "--host", "0.0.0.0", "--port", "3001", "delivery.be.entry:app"]

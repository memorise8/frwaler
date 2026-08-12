# Libertree LLM deployment

GPU 0에서 Qwen3 FP8을 vLLM으로 실행하고, 인증·속도 제한 프록시만 사설망에 공개한다.
운영 DB, Elasticsearch(9200), Cloudflare Tunnel을 다루지 않는 독립 패키지다.

## Pinned runtime

- model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` at a tested immutable revision (official Qwen, Apache-2.0)
- server/proxy: tested vLLM 0.11.0 and Nginx 1.27.5 image digests
- context: 16,384 tokens; initial concurrency: 1
- GPU memory utilization: 0.95 with 1GiB CPU weight offload and an explicit 1.6GiB KV cache
  (16K requires 1.50GiB; the fixed cache avoids consuming all remaining VRAM)
- vLLM: loopback `127.0.0.1:8000`; authenticated proxy: `0.0.0.0:8088`

## L1 install

```bash
cd delivery/llm
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# Put the printed value in .env as LLM_API_KEY. Never commit or paste it into logs.
chmod 600 .env
./scripts/install.sh
./scripts/verify.sh
```

`install.sh` refuses non-GPU-0 deployments, occupied ports, port 9200, exposed secret files,
example/short keys, unavailable Docker/GPU, and less than 150GB free before the estimated 50GB
download. `verify.sh` then requires at least 100GB to remain. Model and compile caches survive restarts.

Status and logs (logs may contain infrastructure metadata, but request/response bodies are not logged):

```bash
docker compose --env-file .env -f compose.yml ps
docker compose --env-file .env -f compose.yml logs --tail=100 vllm-gpu0
```

## Restart and rollback

```bash
docker compose --env-file .env -f compose.yml restart
docker compose --env-file .env -f compose.yml down
```

`down` preserves host caches and logs. To roll back, check out the previous deployment commit,
keep the untracked `.env`, then run `pull` and `up -d`. Never use `down -v` or delete the cache
until the active model and rollback requirements have been confirmed.

## Read-only benchmark

Create a JSONL snapshot containing only `id` and `text`, then run:

```bash
set -a; source .env; set +a
./scripts/benchmark_translation.py samples.jsonl results/benchmark.jsonl --limit 20 \
  --summary results/benchmark-summary.json
```

The script never connects to a DB and refuses to overwrite a prior result. Input selection from
the real PostgreSQL must be a separate read-only query; result approval is required before any
translation job is registered.

`results/` and `samples/` are ignored by Git because they can contain source documents. The
aggregate summary contains only counts, latency, token usage, and protected-token preservation.

For the product's structured Korean-summary contract, use `scripts/benchmark_summary.py` with the
same ignored sample and result paths. It validates JSON shape, Korean output, finish reason, and
latency without registering a Delivery job.

## Network boundary

The proxy permits only `/health`, `/v1/models`, and `/v1/chat/completions`. API routes require a
Bearer token, are limited to 30 requests/minute, two concurrent connections per client, and 2MB
bodies. `/health` is intentionally unauthenticated for health checks. Restrict TCP 8088 to the
current tunnel host at the firewall before any public tunnel route is added in L4.

# L1 GPU 0 validation — 2026-08-12

Target: `ruci@192.168.0.8` (`temis-devlop-server`), GPU 0 only.

## Validated artifacts

- Qwen model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- model revision: `5a5a776300a41aaa681dd7ff0106608ef2bc90db`
- vLLM: `v0.11.0`, digest `sha256:014a95f21c9edf6abe0aea6b07353f96baa4ec291c427bb1176dc7c93a85845c`
- Nginx: `1.27.5-alpine`, digest `sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10`
- Docker Compose user plugin: `v5.1.4`, installed from the official release after checksum verification

## Measured configuration

- GPU memory utilization: `0.95`
- CPU weight offload: `1GiB`
- explicit KV cache: `1.6GiB` (`1717986918` bytes)
- context: `16,384`
- maximum sequences: `1`
- model load: `29.0972GiB` before CPU offload tuning
- steady GPU 0: `31,286MiB` used, `826MiB` free
- GPU 1: `2MiB` used; no LLM container assigned

The initial `0.90` setting had no KV cache. At `0.97`, vLLM measured `1.09GiB`, below the
`1.50GiB` required for 16K. A higher utilization crossed the driver's startup reservation.
One GiB CPU offload provided enough space, and an explicit 1.6GiB cache avoided vLLM consuming
all otherwise available VRAM.

## Verification results

- vLLM health: healthy
- proxy health: healthy
- unauthenticated `/v1/models`: `401`
- authenticated `/v1/models`: success
- `/v1/chat/completions`: Korean translation success
- protected `2025` and `https://example.org/report.pdf`: preserved
- restart ready: `28s`
- cached weight load: `4.12s`
- cached compiled graph load: `0.978s`
- Hugging Face cache: `39GB`
- vLLM compile cache: `89MB`
- disk remaining: `118GB`

Bindings after validation:

```text
0.0.0.0:8088   authenticated proxy
127.0.0.1:8000 vLLM
127.0.0.1:9200 existing Elasticsearch
```

Elasticsearch, Cloudflare Tunnel, SQLite, and PostgreSQL were not modified. No DB benchmark or
translation job was run. The generated `.env` and API key remain only on the GPU server with mode
`0600` and are not recorded here.

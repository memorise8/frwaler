# L2 read-only translation benchmark — 2026-08-12

## Scope and safety

- Source: the real Delivery PostgreSQL, queried with `default_transaction_read_only=on`
- Database writes, schema changes, translation job registration: none
- Result JSONL: stored only in the ignored `delivery/llm/results/` path on the GPU server
- Committed record: aggregate measurements only; no source or translated document text
- GPU: GPU 0 only; GPU 1 remained idle

The deterministic sample contained 20 public document records: two each from `da`, `de`, `el`,
`en`, `es`, `fr`, `it`, `ja`, `nl`, and `zh-cn`. Length buckets were short 6, medium 6, long 6,
and xlong 2. Input length ranged from 277 to 9,151 characters. Each input included its source URL
to test preservation.

## Measurements

| Metric | Result |
|---|---:|
| Completed samples | 20 / 20 |
| Empty outputs | 0 |
| Outputs containing Korean | 20 / 20 |
| Exposed thinking markers | 0 |
| Minimum latency | 1.020s |
| Median latency | 7.411s |
| Maximum latency | 33.151s |
| Total sequential time | 195.526s |
| Prompt tokens | 20,195 |
| Completion tokens | 15,124 |
| Effective completion throughput | 77.35 tokens/s |
| Exact protected-token preservation | 72 / 85 (84.71%) |
| Separator-normalized preservation | 81 / 85 (95.29%) |
| vLLM/proxy restarts | 0 / 0 |

Nine of the thirteen exact differences were punctuation or localized number formatting. Four were
real omissions in two samples: one number, two dates, and one URL. The omissions occurred in one
Greek xlong sample and one Italian short sample, so they cannot be explained only by output length.

## Decision

L2 validates the endpoint, multilingual Korean output, 16K request path, latency measurement, and
stable GPU 0 operation. It does **not** approve bulk translation because protected-token fidelity
is below the required lossless threshold. Keep large batches disabled.

Before L5, strengthen the translation pipeline so URLs/dates/numbers are protected with placeholders
and deterministically restored, then rerun the two failing samples plus the same 20-record suite.
Human review is still required for terminology, institution names, fluency, and semantic fidelity.

After the run, both containers were healthy, GPU 1 remained unused, disk free space was 118GB, and
the existing Elasticsearch and Cloudflare Tunnel were unchanged.

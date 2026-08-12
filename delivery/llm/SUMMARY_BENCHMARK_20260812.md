# Structured Korean summary benchmark — 2026-08-12

The same deterministic 20-record, 10-language read-only snapshot from L2 was used. No database
connection, write, schema change, or Delivery job registration occurred during inference.

## First pass

| Metric | Result |
|---|---:|
| Valid JSON contract | 20 / 20 |
| Korean summary | 19 / 20 |
| Finish reason `stop` | 20 / 20 |
| Thinking markers | 0 |
| Median latency | 9.335s |
| Maximum latency | 13.214s |
| Total sequential time | 180.727s |
| Prompt tokens | 21,290 |
| Completion tokens | 6,606 |

Compared with the prior full-translation benchmark, completion tokens fell from 15,124 to 6,606
(56.3% lower), while total sequential time fell from 195.526s to 180.727s. All results contained
five or fewer key points and a valid institutions array.

One `zh-cn` short sample returned a structurally valid non-Korean summary. The provider contract was
therefore strengthened to require Hangul in `summary_ko` and every non-empty key point. Such output
is now a retryable `invalid_response`, never a completed job.

## Targeted retry

The failing sample was rerun with the strengthened prompt:

- valid contract: 1 / 1
- Korean summary: 1 / 1
- finish reason: `stop`
- latency: 8.853s
- service restarts: 0

## Decision

The machine contract and Korean-output guard are ready for application integration. This benchmark
does not replace human review of meaning, terminology, institution extraction, or hallucination.
Keep production bulk jobs disabled and follow preview → 5 records → review → 20 records before L5.

GPU 0 remained healthy, GPU 1 remained unused, and Elasticsearch, Cloudflare Tunnel, SQLite, and
the real PostgreSQL were not modified.

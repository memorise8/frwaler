# Delivery MVP runtime audit

> Audit date: 2026-08-12 (Asia/Seoul)  
> Branch: `delivery-mvp-audit-20260812`  
> Safety boundary: no writes to the operational SQLite database or the
> migrated real-data PostgreSQL database.

## 1. Docker access

- The Codex process had supplementary group `973(docker)` after the host
  reboot.
- `docker ps` succeeded.

## 2. Running real-data stack (read-only)

The running `libertree-delivery` containers were inspected without restarting
or reconfiguring them. The BE returned `{"status":"ok"}`. PostgreSQL queries
were executed inside an explicit `BEGIN READ ONLY` transaction.

| Measurement | Result |
| --- | ---: |
| PostgreSQL health | healthy |
| BE status | running / health endpoint OK |
| Worker status | running |
| Sites | 805 |
| Documents | 536,017 |
| Crawl jobs | 0 |
| Latest `collected_at` | 2026-08-06 09:02:30 UTC |

No operational crawler job was enqueued. The real-data PostgreSQL DSN was not
exported as `TEST_PG_DSN` and the operational SQLite database was not opened.

## 3. Disposable PostgreSQL integration tests

A dedicated `postgres:16` container used database `libertree_audit` on host
port 55433. Tests ran from the delivery BE image with a DSN that named only
that disposable database. The container was deleted afterward.

The following 29 tests passed:

- PostgreSQL document storage and deduplication: 8
- BaseCrawler PostgreSQL and SQLite-backend regression: 2
- Worker queue management: 9
- Worker execution: 3
- FastAPI endpoints and job management: 7

## 4. Representative crawler E2E

The successful run used Compose project `libertree-e2e-audit20260812c`,
database `libertree_e2e_audit20260812c`, dedicated `pgdata` and `blob` volumes,
BE port 19080, and FE port 19000. The script verified the Compose project label
and database name before its first seed write.

| Path | Site ID | Saved | Result |
| --- | --- | ---: | --- |
| HTML | `cedelft-eu-reports` | 3 | PASS |
| API | `repository-lboro-ac-uk` | 3 | PASS |
| Paper | `hrbopenresearch-org` | 3 | PASS |
| Report | `krihs-re-kr-krihslibraryreport` | 3 | PASS |
| Playwright | `government-se-publications` | 3 | PASS |

This proves the FE proxy → BE queue → Worker crawler → PostgreSQL path for all
five representative categories. The project containers and volumes were
removed by the script's exit trap.

Two guard-script defects were found before a successful run and fixed in
separate commits: an escaped Docker inspect template and use of psql variables
through `-c`, where they were not expanded. Both failures stopped before any
write outside the disposable project.

## 5. Incremental rerun duplicate prevention

A second dedicated PostgreSQL database, `libertree_incremental` on host port
55434, ran a Worker-level integration test:

1. A full job returned the same two deterministic documents and saved 2 rows.
2. An incremental job returned those same two documents again.
3. The incremental job completed with `saved_count=0`.
4. The site's document count remained 2.

The database-level `(site_id, post_number, meta_url)` deduplication therefore
prevents duplicate rows during an incremental rerun.

Current limitation: Worker records and reports the requested mode, but
`run_job()` currently invokes every crawler through `crawl(limit=...)` without
passing `mode` or a high-water mark. Incremental mode prevents duplicate
storage, but does not yet guarantee reduced network scanning. That optimization
should be designed separately because crawler interfaces are heterogeneous.

## 6. Audit commits

- `294ac71` — repair the disposable-project Docker label guard.
- `5e8676c` — make psql variable expansion reliable in E2E result validation.
- `c3e94d9` — add incremental rerun deduplication regression coverage.
- Final documentation commit — this audit record.

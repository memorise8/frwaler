# Libertree catalogue runtime

This directory owns the isolated, catalogue-only runtime. It is intentionally separate from the repository-root Finolaw Docker and Supervisor configuration; it does not change port 3001 ownership. The compose file publishes only to loopback and defaults to host port `3101` for QA.

## Required inputs

Run from `libertree-app/` with these environment variables:

- `LIBERTREE_HOST_APP_DATA_ROOT`: absolute host path to `libertree-app/data/`, which contains the physical canonical `libertree.db` and any live SQLite `-wal`/`-shm` sidecars.
- `LIBERTREE_HOST_BLOB_ROOT`: absolute host path to the repository-root `libertree/` directory.
- `ADMIN_USER` and `ADMIN_PASSWORD`: Basic-auth credentials.
- `LIBERTREE_HOST_PORT` (optional): unused loopback host port; default `3101`.

The container maps those sources only to `/data/repository/libertree-app/data` and `/data/repository/libertree`, both read-only. Mounting the data directory (rather than only the main database file) is required: a normal read-only SQLite connection must be able to see live WAL/SHM sidecars. It sets only the three explicit `LIBERTREE_*` contract values plus Basic-auth credentials. The startup gate rejects a writable/missing data mount, a changed data-path value, and crawler, Qwen, or external-AI key environment variables.

Build and QA without replacing the existing service:

```sh
docker compose -f docker-compose.yml build
docker compose -f docker-compose.yml up -d
curl --fail --user "$ADMIN_USER:$ADMIN_PASSWORD" http://127.0.0.1:3101/api/health
docker compose -f docker-compose.yml down
```

`/api/health` is a protected readiness probe, not a liveness-only endpoint. It checks Basic auth and performs a minimal query through the same non-immutable, read-only catalogue connection used by `/search`; the preflight still uses immutable access only to validate the checkpointed schema. Misconfiguration or unavailable live catalogue access returns `503`, wrong or missing credentials return `401`, and only a usable catalogue returns `200`.

## Intentional route removals

The only application routes are `/`, `/search`, `/search/:id`, `/api/blob/:seq_id/:ext`, and `/api/health`. There is no crawler scheduler or Supervisor program in this runtime.

After authentication, these excluded routes deliberately have no handler and return `404`: `/api/crawler`, `/api/auto-add`, `/api/smart-find`, `/crawler`, `/auto-add`, `/smart-find`, `/admin`, `/admin/status`, `/admin/summary`, and `/admin/collection-report`. Without valid Basic auth, the app-wide authentication proxy returns `401` before route matching; this is intentional and must not be mistaken for a restored legacy route.

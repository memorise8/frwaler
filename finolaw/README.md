# Finolaw Frontend

Finolaw is the active Next.js UI for the crawler-poc project. The legacy `web/` frontend has been removed; use this app for dashboard, search, Smart Find, Auto-Add, and crawler management.

## Quick Start

Run from this directory:

```bash
npm install
npm run dev
```

Open http://localhost:3001.

## Project Layout

- `src/app/` — App Router pages and API routes
- `src/lib/db.ts` — readonly SQLite access to `../data/papers.db`
- `src/lib/*runner*.ts` — spawns crawler commands through `../.venv/bin/python`
- `src/proxy.ts` — HTTP Basic Auth gate using `ADMIN_USER` and `ADMIN_PASSWORD`

## Required Parent Project Files

This app expects to live inside the crawler-poc repo and depends on sibling paths:

- `../crawler/` — Python crawler package
- `../.venv/bin/python` — Python used by crawler jobs
- `../data/papers.db` — SQLite database for dashboard/search
- `../.cache/` — crawler and Auto-Add logs
- `../crawler/.env` — `OPENAI_API_KEY` for Auto-Add/Codex flows

If `../data/papers.db` is missing, the UI can start but dashboard/search data will be empty.

## Scripts

```bash
npm run dev      # Next.js dev server on port 3001
npm run build    # production build
npm run start    # production server on port 3001
npm run lint     # ESLint
```

## Validation Checklist

From `finolaw/`:

```bash
npm run lint
npm run build
curl -i http://localhost:3001 | head
```

From the repository root:

```bash
.venv/bin/python -m crawler.main stats
.venv/bin/python -m crawler.main list-sites
```

If `../data/papers.db` is missing, crawler stats/search-related commands may show no data or initialize an empty DB. Run a small crawl or Auto-Add flow first.

## Next.js 16 Note

This project uses Next.js 16. Before changing framework APIs, file conventions, proxy behavior, or route handlers, check the local docs under `node_modules/next/dist/docs/`.

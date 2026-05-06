# Finolaw Crawler

Finolaw is a Docker-deployed crawler management app. It runs a Next.js UI, Python crawlers, Playwright, and Codex CLI in one container.

## Quick Start

Run from the repository root.

```bash
docker compose build
docker compose up -d
docker compose ps
```

Open:

```text
http://<server-ip>:3001
```

The container serves the app with `next start` on port `3001`.

## Runtime Paths

These paths are bind-mounted and are managed per deployment/server.

| Path | Purpose | Git policy |
|------|---------|------------|
| `data/data.db` | SQLite database | ignored |
| `.cache/` | UI, crawler, and Codex logs | ignored |
| `crawler/sites/custom/*.py` | Codex-generated custom crawlers | ignored |
| `crawler/sites/configs/*.json` | Tier 1 generated JSON configs | ignored |
| `${HOME}/.codex` | Server admin's Codex OAuth state | not in repo |

Built-in shared crawlers stay in Git under `crawler/sites/*.py`.

## Codex OAuth

Codex-based Auto-Add uses the server admin's local Codex OAuth login, not `OPENAI_API_KEY`.

On each deployment server, log in as the user that runs `docker compose`, then run:

```bash
codex login --device-auth
codex login status
```

The Compose file mounts that user's Codex state into the container:

```yaml
  - ${HOME}/.codex:/home/codex/.codex
```

Verify the container can see the login:

```bash
docker compose exec app codex login status
```

Expected result:

```text
Logged in using ChatGPT
```

## API Keys

`OPENAI_API_KEY` is still used by features that call the OpenAI API directly, such as GPT-based Smart Find and the Tier 1 AutoAddAgent path.

Codex Auto-Add, the default crawler-generation path, uses Codex CLI OAuth instead.

Configure API keys in `crawler/.env` when needed:

```env
OPENAI_API_KEY=sk-...
LLM_PROVIDER=gpt
# GEMINI_API_KEY=...
```

## Auto-Add Flow

The default crawler-generation path is Tier 2 Codex CLI.

```text
/auto-add
  -> /api/auto-add/codex
  -> python -m crawler.main auto-add-codex <url>
  -> codex exec
  -> crawler/sites/custom/<site_id>.py
```

Generated crawlers are runtime files. Back them up, but do not commit them unless they are promoted to a shared built-in crawler.

Generated crawlers can be removed from the crawler management page. Select a runtime crawler, then click `생성 크롤러 삭제`. This deletes only the generated file under `crawler/sites/custom/` or `crawler/sites/configs/`; built-in shared crawlers and collected DB rows are not deleted.

## Product CSV Import

For catalog sites that block automated crawling, use `/products/import`. Select multiple page-level CSV exports at once; the app merges them into the `products` table and upserts duplicates by Mouser part number or manufacturer part number.

## Useful Commands

```bash
# Health
curl http://localhost:3001/api/health

# List registered crawler sites
docker compose exec app .venv/bin/python -m crawler.main list-sites

# Run a built-in crawler
docker compose exec app .venv/bin/python -m crawler.main crawl ntrs --limit 3

# Check Codex auth inside Docker
docker compose exec app codex login status

# View logs
docker compose logs -f --tail=200
```

## Backup

Back up deployment runtime state regularly.

```bash
tar czf backup-$(date +%F).tar.gz \
  data \
  .cache \
  crawler/sites/custom \
  crawler/sites/configs
```

Codex OAuth state lives in `${HOME}/.codex`. Treat it as sensitive account state and manage it according to your server access policy.

## More Docs

- [Install Guide](README-install.md)
- [Auto-Add Guide](docs/auto-add-usage.md)
- [Security Model](docs/security-model.md)
- [Current Status](docs/status.md)

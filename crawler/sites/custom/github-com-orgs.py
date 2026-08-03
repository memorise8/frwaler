# -*- coding: utf-8 -*-
"""GitHub organization repositories crawler for Deltares.

Starting URL: https://github.com/orgs/Deltares/repositories
API endpoint:  https://api.github.com/orgs/Deltares/repos
"""

import base64
import json
import os
import re
import sys
import time

# spec_from_file_location has no package context — use absolute import
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler


class GitHubComOrgsCrawler(BaseCrawler):
    """Crawler for Deltares GitHub organization repositories."""

    site_id = "github-com-orgs"
    site_name = "Custom: github-com-orgs"
    base_url = "https://github.com"

    _ORG = "Deltares"
    _API_BASE = "https://api.github.com"
    _PER_PAGE = 100
    _WALL_CLOCK_LIMIT_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay)
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_next_link(link_header: str) -> str | None:
        """Extract the 'next' URL from a GitHub Link response header."""
        if not link_header:
            return None
        for part in link_header.split(","):
            m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Strip markdown syntax, returning readable plain text."""
        text = re.sub(r"```[\s\S]*?```", " ", text)
        text = re.sub(r"`[^`\n]+`", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"^\|[-:| ]+\|$", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[-=*_]{3,}\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _fetch_readme(self, repo_name: str) -> str:
        """Fetch and decode the README for a repo, returning stripped plain text."""
        url = f"{self._API_BASE}/repos/{self._ORG}/{repo_name}/readme"
        for attempt in range(3):
            try:
                time.sleep(self._delay)
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 404:
                    return ""
                if resp.status_code == 403:
                    reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
                    wait = max(5, reset_ts - int(time.time()))
                    print(f"[github-com-orgs] Rate limited (README). Waiting {min(wait, 60)}s...")
                    time.sleep(min(wait, 60))
                    continue
                if resp.status_code != 200:
                    if attempt < 2:
                        time.sleep((attempt + 1) * 3)
                    continue
                data = resp.json()
                encoded = data.get("content", "")
                if not encoded:
                    return ""
                raw_bytes = base64.b64decode(encoded)
                try:
                    raw_text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raw_text = raw_bytes.decode("utf-8", errors="replace")
                return self._strip_markdown(raw_text)
            except Exception as exc:
                print(f"[github-com-orgs] README {repo_name} error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
        return ""

    @staticmethod
    def _iso_to_date(iso_str: str) -> str:
        """'2026-03-30T13:30:40Z' → '2026-03-30'."""
        if not iso_str:
            return ""
        return iso_str[:10]

    def _build_abstract(self, repo: dict) -> str:
        """Build a >= 100-char abstract from description + README + metadata fallback."""
        description = (repo.get("description") or "").strip()
        repo_name = repo.get("name", "")
        full_name = repo.get("full_name", "") or repo_name

        abstract = description

        # Supplement with README when description is short
        if len(abstract) < 100:
            readme = self._fetch_readme(repo_name)
            if readme:
                abstract = (abstract + "\n\n" + readme[:800]).strip() if abstract else readme[:800]

        # Metadata fallback — always produces >= 100 chars
        if len(abstract) < 100:
            language = repo.get("language") or ""
            topics = ", ".join(repo.get("topics") or [])
            stars = repo.get("stargazers_count", 0)
            forks = repo.get("forks_count", 0)
            created = (repo.get("created_at") or "")[:10]
            homepage = (repo.get("homepage") or "").strip()

            parts = [description] if description else []
            parts.append(
                f"{full_name} is an open-source repository maintained by the "
                f"Deltares organisation on GitHub."
            )
            if language:
                parts.append(f"Primary language: {language}.")
            if topics:
                parts.append(f"Topics: {topics}.")
            parts.append(f"Created: {created}. Stars: {stars}. Forks: {forks}.")
            if homepage:
                parts.append(f"Homepage: {homepage}.")
            abstract = " ".join(parts)

        return abstract.strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Deltares GitHub organisation repositories via the REST API.

        Paginates through /orgs/Deltares/repos using Link: rel="next" headers.
        For each repo, fetches the README to build a rich abstract.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        next_url = (
            f"{self._API_BASE}/orgs/{self._ORG}/repos"
            f"?per_page={self._PER_PAGE}&sort=created&direction=desc&page=1"
        )

        while next_url:
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_LIMIT_S:
                print(f"[github-com-orgs] 25-minute wall-clock limit reached ({elapsed:.0f}s). Stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 200:
                print(f"[github-com-orgs] Safety cap of 200 pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[github-com-orgs] page {page}: saved {saved}/{limit_display}")

            # Fetch list page with retry + exponential backoff
            repos = None
            link_header = ""
            for attempt in range(3):
                try:
                    time.sleep(self._delay)
                    resp = self._session.get(next_url, timeout=30)
                    if resp.status_code == 403:
                        reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
                        wait = max(5, reset_ts - int(time.time()))
                        print(f"[github-com-orgs] Rate limited (list). Waiting {min(wait, 60)}s...")
                        time.sleep(min(wait, 60))
                        continue
                    resp.raise_for_status()
                    repos = resp.json()
                    link_header = resp.headers.get("Link", "")
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[github-com-orgs] List page {page} error (attempt {attempt+1}/3): {exc}")
                    if attempt < 2:
                        time.sleep((attempt + 1) * 3)

            if repos is None:
                print(f"[github-com-orgs] Failed to fetch page {page} after 3 retries. Stopping.")
                break

            if not repos:
                print(f"[github-com-orgs] No repos on page {page}. Done.")
                break

            next_url = self._parse_next_link(link_header)

            for repo in repos:
                if limit is not None and saved >= limit:
                    break

                try:
                    repo_url = repo.get("html_url", "")
                    if not repo_url or repo_url in seen_urls:
                        continue
                    seen_urls.add(repo_url)

                    repo_id = repo.get("id")
                    repo_name = repo.get("name", "")
                    full_name = repo.get("full_name", "")
                    description = (repo.get("description") or "").strip()
                    created_at = repo.get("created_at", "")
                    updated_at = repo.get("updated_at", "")
                    pushed_at = repo.get("pushed_at", "")
                    language = repo.get("language") or ""
                    topics = repo.get("topics") or []
                    stars = repo.get("stargazers_count", 0)
                    forks = repo.get("forks_count", 0)
                    open_issues = repo.get("open_issues_count", 0)
                    license_name = (repo.get("license") or {}).get("name", "")
                    homepage = (repo.get("homepage") or "").strip()
                    is_fork = repo.get("fork", False)
                    is_archived = repo.get("archived", False)
                    size_kb = repo.get("size", 0)

                    abstract = self._build_abstract(repo)

                    if len(abstract.strip()) < 50:
                        print(f"[github-com-orgs] Skipping {repo_name}: abstract too short ({len(abstract)} chars)")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(repo_id),
                        "post_number": str(repo_id),
                        "title": full_name or repo_name,
                        "abstract": abstract.strip(),
                        "published_date": self._iso_to_date(created_at),
                        "listed_date": self._iso_to_date(updated_at),
                        "posted_date": self._iso_to_date(updated_at),
                        "authors": (repo.get("owner") or {}).get("login", self._ORG),
                        "publisher": self._ORG,
                        "department": "",
                        "journal": "",
                        "url": repo_url,
                        "pdf_url": None,
                        "doi": None,
                        "keywords": ",".join(topics) if topics else language,
                        "category": language,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": updated_at,
                            "full_name": full_name,
                            "description": description,
                            "language": language,
                            "topics": topics,
                            "stars": stars,
                            "forks": forks,
                            "open_issues": open_issues,
                            "license": license_name,
                            "homepage": homepage,
                            "is_fork": is_fork,
                            "is_archived": is_archived,
                            "size_kb": size_kb,
                            "created_at": created_at,
                            "updated_at": updated_at,
                            "pushed_at": pushed_at,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[github-com-orgs] Saved {saved}/{limit_display}: {repo_name}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[github-com-orgs] Item {repo.get('name', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[github-com-orgs] Done. Total saved: {saved}")
        return saved

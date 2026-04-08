# -*- coding: utf-8 -*-
"""Abstract base crawler."""

import json as _json
import os
import random
import time
import uuid
from abc import ABC, abstractmethod
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import db as db_module


class BaseCrawler(ABC):
    """Abstract base class for all site crawlers."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, db_conn, delay=1.0, cookies=None, respect_robots=True):
        self._conn = db_conn
        self._delay = delay
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })

        # --- Feature 1: robots.txt compliance ---
        self._respect_robots = respect_robots
        self._robots_cache = {}

        # --- Feature 3: Dynamic rate limiting ---
        self._min_delay = delay
        self._max_delay = max(delay * 10, 30)
        self._current_delay = delay
        self._success_streak = 0

        # --- Feature 4: Cookie/Session management ---
        self._cookie_jar_path = None
        if cookies:
            self._session.cookies.update(cookies)

    # ------------------------------------------------------------------
    # Abstract properties / methods
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def site_id(self) -> str:
        """Short identifier for this site (e.g. 'ntrs')."""

    @property
    @abstractmethod
    def site_name(self) -> str:
        """Human-readable site name."""

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Base URL of the site."""

    @abstractmethod
    def crawl(self, limit=None):
        """Crawl the site and persist papers to the DB.

        Parameters
        ----------
        limit:
            Maximum number of papers to save. ``None`` means unlimited.
        """

    # ------------------------------------------------------------------
    # Feature 1: robots.txt compliance
    # ------------------------------------------------------------------

    def _check_robots(self, url):
        """Check if the URL is allowed by the site's robots.txt.

        Returns True if allowed or on error (permissive by default).
        """
        if not self._respect_robots:
            return True
        try:
            parsed = urlparse(url)
            domain = f"{parsed.scheme}://{parsed.netloc}"
            if domain not in self._robots_cache:
                rp = RobotFileParser()
                robots_url = f"{domain}/robots.txt"
                rp.set_url(robots_url)
                try:
                    rp.read()
                except Exception:
                    # No robots.txt or timeout — allow by default
                    self._robots_cache[domain] = None
                    return True
                self._robots_cache[domain] = rp
            rp = self._robots_cache[domain]
            if rp is None:
                return True
            return rp.can_fetch(self.USER_AGENT, url)
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Feature 3: Dynamic rate limiting
    # ------------------------------------------------------------------

    def _adjust_delay(self, response_time, status_code):
        """Adjust the current delay based on server response behaviour."""
        if status_code == 429:
            # Rate limited — double delay
            self._current_delay = min(self._current_delay * 2, self._max_delay)
            self._success_streak = 0
            print(f"[{self.site_id}] Rate limited (429). Delay increased to {self._current_delay:.1f}s")
            return

        if status_code and 200 <= status_code < 400:
            self._success_streak += 1
        else:
            self._success_streak = 0

        if response_time > 3:
            # Slow response — increase delay by 50%
            self._current_delay = min(self._current_delay * 1.5, self._max_delay)
            self._success_streak = 0
            print(f"[{self.site_id}] Slow response ({response_time:.1f}s). Delay increased to {self._current_delay:.1f}s")
        elif self._success_streak >= 5:
            # Good streak — decrease delay by 10%
            self._current_delay = max(self._current_delay * 0.9, self._min_delay)

    # ------------------------------------------------------------------
    # Feature 4: Cookie/Session management
    # ------------------------------------------------------------------

    def _init_session(self, cookies=None, headers=None):
        """Re-initialise session with custom cookies and/or headers.

        Parameters
        ----------
        cookies : dict or None
            Extra cookies to set on the session.
        headers : dict or None
            Extra headers to merge into the session.
        """
        if cookies:
            self._session.cookies.update(cookies)
        if headers:
            self._session.headers.update(headers)
        # Load persisted cookies if a jar path is configured
        if self._cookie_jar_path and os.path.isfile(self._cookie_jar_path):
            try:
                with open(self._cookie_jar_path, "r", encoding="utf-8") as fh:
                    saved = _json.load(fh)
                self._session.cookies.update(saved)
                print(f"[{self.site_id}] Loaded cookies from {self._cookie_jar_path}")
            except Exception as exc:
                print(f"[{self.site_id}] Failed to load cookies: {exc}")

    def _save_cookies(self):
        """Persist current session cookies to disk (JSON)."""
        if not self._cookie_jar_path:
            return
        try:
            cookie_dict = dict(self._session.cookies)
            os.makedirs(os.path.dirname(self._cookie_jar_path) or ".", exist_ok=True)
            with open(self._cookie_jar_path, "w", encoding="utf-8") as fh:
                _json.dump(cookie_dict, fh, ensure_ascii=False)
        except Exception as exc:
            print(f"[{self.site_id}] Failed to save cookies: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _request(self, url, params=None, method="GET", retries=3, **kwargs):
        """Make an HTTP request with rate-limiting, retries, and error handling.

        Includes robots.txt compliance, exponential backoff, dynamic rate
        limiting, and automatic cookie handling.

        Returns the ``requests.Response`` on success, or ``None`` on error.
        """
        # Feature 1: robots.txt check
        if not self._check_robots(url):
            print(f"[{self.site_id}] Blocked by robots.txt: {url}")
            return None

        for attempt in range(retries):
            # Feature 3: use dynamic delay
            time.sleep(self._current_delay)
            try:
                start_time = time.monotonic()
                response = self._session.request(
                    method, url, params=params, timeout=30,
                    allow_redirects=True, **kwargs
                )
                elapsed = time.monotonic() - start_time

                # Feature 2: retry on specific status codes
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt + random.uniform(0, 1), 60)
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", wait)
                        wait = float(retry_after) if str(retry_after).isdigit() else wait
                    # Feature 3: adjust delay for server errors
                    self._adjust_delay(elapsed, response.status_code)
                    print(
                        f"[{self.site_id}] Server error {response.status_code}, "
                        f"retrying in {wait:.1f}s..."
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()

                # Feature 3: adjust delay on success
                self._adjust_delay(elapsed, response.status_code)

                # Feature 4: persist cookies after successful request
                self._save_cookies()

                return response
            except requests.RequestException as exc:
                print(
                    f"[{self.site_id}] Request error "
                    f"(attempt {attempt + 1}/{retries}) for {url}: {exc}"
                )
                if attempt < retries - 1:
                    # Feature 2: exponential backoff
                    wait = min(2 ** attempt + random.uniform(0, 1), 60)
                    print(f"[{self.site_id}] Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    return None

    def _save_paper(self, paper_dict):
        """Persist a paper dict to the database, skipping duplicates.

        Ensures the required ``id`` field is present (generates a UUID if not).
        Returns True if the paper was saved, False if it was a duplicate.
        """
        if not paper_dict.get("id"):
            paper_dict["id"] = str(uuid.uuid4())
        paper_dict.setdefault("site_id", self.site_id)

        # Duplicate check
        url = paper_dict.get("url", "")
        title = paper_dict.get("title", "")
        if url or title:
            content_hash = db_module.compute_content_hash(title, url)
            if db_module.paper_exists(self._conn, self.site_id, url=url, title_hash=content_hash):
                print(f"[{self.site_id}] Skipping duplicate: {title[:50]}")
                return False
            # Store hash as external_id if not already set
            if not paper_dict.get("external_id"):
                paper_dict["external_id"] = content_hash

        db_module.upsert_paper(self._conn, paper_dict)
        return True

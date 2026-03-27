# -*- coding: utf-8 -*-
"""Abstract base crawler."""

import time
import uuid
from abc import ABC, abstractmethod

import requests

from . import db as db_module


class BaseCrawler(ABC):
    """Abstract base class for all site crawlers."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, db_conn, delay=1.0):
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
    # Helpers
    # ------------------------------------------------------------------

    def _request(self, url, params=None, method="GET", retries=3, **kwargs):
        """Make an HTTP request with rate-limiting, retries, and error handling.

        Returns the ``requests.Response`` on success, or ``None`` on error.
        """
        for attempt in range(retries):
            time.sleep(self._delay)
            try:
                response = self._session.request(
                    method, url, params=params, timeout=30, **kwargs
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                print(f"[{self.site_id}] Request error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    return None

    def _save_paper(self, paper_dict):
        """Persist a paper dict to the database via db.upsert_paper.

        Ensures the required ``id`` field is present (generates a UUID if not).
        """
        if not paper_dict.get("id"):
            paper_dict["id"] = str(uuid.uuid4())
        paper_dict.setdefault("site_id", self.site_id)
        db_module.upsert_paper(self._conn, paper_dict)

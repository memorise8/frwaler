# -*- coding: utf-8 -*-
"""Abstract base crawler."""

import time
import uuid
from abc import ABC, abstractmethod

import requests

from . import db as db_module
from . import storage as storage_module


class CrawlControl(BaseException):
    """Control-flow signals that must pass through crawler code untouched.

    Deliberately BaseException, not Exception: 733 of the 800 site crawlers
    wrap their save loop in `except Exception: continue`, which would swallow
    a RuntimeError-based signal and defeat both cooperative cancellation and
    the incremental early stop. The corpus already re-raises KeyboardInterrupt
    (735/800), so BaseException-shaped control flow is the established escape
    hatch. Only the delivery worker may catch these.
    """


class CrawlCancelled(CrawlControl):
    """Cooperative stop raised at safe request/save boundaries."""


class CrawlUpToDate(CrawlControl):
    """Incremental crawl reached already-collected territory (newest-first only).

    Raised from _save_paper_v2 ON the UP_TO_DATE_THRESHOLD-th consecutive
    already-known document (the first N-1 return silently). Only effective on
    the libertree/v2 save path -- the legacy sqlite path never counts.
    """


class BaseCrawler(ABC):
    """Abstract base class for all site crawlers."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # 재개(cursor) 계약 -- 기본값이면 아무것도 달라지지 않는다.
    DELIVERY_ORDER = "arbitrary"      # newest_first | oldest_first | arbitrary
    UP_TO_DATE_THRESHOLD = 50

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
        self.delivery_mode = "incremental"
        self.delivery_should_cancel = lambda: False
        self._last_save_created = False
        self.delivery_cursor = None      # 워커가 주입하는 시작점 (읽기 전용으로 쓸 것)
        self._pending_cursor = None      # _advance_cursor 가 기록, 워커가 회수해 저장
        self._cursor_items_done = 0
        self._consecutive_known = 0

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

    def _check_cancelled(self):
        if self.delivery_should_cancel():
            raise CrawlCancelled("crawl cancellation requested")

    def _advance_cursor(self, cursor, items_done=0):
        """페이지 하나를 끝낼 때 호출: 다음 실행이 시작할 지점을 보고한다.

        메모리에만 기록한다 -- DB 저장과 커밋은 워커의 종결 전이에서 일어난다.
        크롤 도중에 저장하면 실패한 크롤의 커서가 남는다.

        cursor 는 스칼라로 이루어진 평평한 dict 여야 한다: dict(cursor) 는 얕은
        복사라 중첩된 값은 보호되지 않는다.
        """
        if cursor is None:
            return
        self._pending_cursor = dict(cursor)
        self._cursor_items_done += int(items_done)

    @property
    def delivery_pending_cursor(self):
        """워커가 종결 전이에서 회수하는, 마지막으로 보고된 커서 (없으면 None)."""
        return self._pending_cursor

    @property
    def delivery_cursor_items_done(self):
        return self._cursor_items_done

    def _request(self, url, params=None, method="GET", retries=3, **kwargs):
        """Make an HTTP request with rate-limiting, retries, and error handling.

        Returns the ``requests.Response`` on success, or ``None`` on error.
        """
        for attempt in range(retries):
            self._check_cancelled()
            time.sleep(self._delay)
            self._check_cancelled()
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
                    self._check_cancelled()
                    time.sleep(wait)
                else:
                    return None

    def _save_paper(self, paper_dict):
        """Persist a crawled item.

        Routing (Phase A — libertree.db):

        - If the bound connection is the new ``data/libertree.db`` (detected
          via the presence of ``documents.seq_id``), the legacy
          ``paper_dict`` is passed through the libertree adapter and then
          mapped onto the new schema's columns (``meta_url``, ``listed_date``,
          ``post_number`` ←``external_id``, etc) and inserted via
          :meth:`_save_paper_v2`. Returns the new ``seq_id``.

        - Otherwise (legacy ``data/papers.db`` connection), the existing
          ``paper_to_document`` adapter + ``upsert_document`` flow is kept
          for backward compatibility with every site crawler that was
          already on the legacy libertree branch. Returns the legacy
          ``documents.id``.
        """
        self._check_cancelled()
        from . import libertree_adapter
        paper_dict.setdefault("site_id", self.site_id)
        doc_dict = libertree_adapter.paper_to_document(paper_dict)

        import os as _os
        if _os.environ.get("LIBERTREE_DB_BACKEND", "sqlite").lower() == "postgres" or self._conn_is_libertree():
            # Map legacy/adapter dict → new libertree.db documents columns.
            v2_doc = {
                "site_id": doc_dict.get("site_id"),
                "post_number": doc_dict.get("external_id"),
                "meta_url": doc_dict.get("meta_url") or doc_dict.get("pdf_url") or "",
                "title": doc_dict.get("title") or "(untitled)",
                "published_date": doc_dict.get("published_date"),
                "listed_date": doc_dict.get("posted_date"),
                "authors": doc_dict.get("authors"),
                "publisher": doc_dict.get("publisher"),
                "journal": doc_dict.get("journal"),
                "pdf_url": doc_dict.get("pdf_url"),
                "keywords": doc_dict.get("keywords"),
                "abstract": doc_dict.get("abstract"),
                "original_filename": doc_dict.get("original_filename"),
                "summary": doc_dict.get("summary"),
            }
            return self._save_paper_v2(v2_doc)

        return self._save_document(doc_dict)

    def _save_paper_legacy(self, paper_dict):
        """Original behaviour: write to the legacy ``papers`` table (UUID PK).

        Kept for opt-in use when a caller specifically needs the old schema.
        """
        if not paper_dict.get("id"):
            paper_dict["id"] = str(uuid.uuid4())
        paper_dict.setdefault("site_id", self.site_id)
        db_module.upsert_paper(self._conn, paper_dict)

    def _save_document(self, doc_dict):
        """Persist a document dict to the ``documents`` table.

        Returns the integer sequence id assigned to (or already held by)
        the row.

        ``pdf_path`` and ``txt_path`` are intentionally NOT written here
        — they get filled in by the download (``cmd_download``) and convert
        (``convert_site_files``) steps respectively. This way ``txt_path``
        stays NULL until conversion succeeds, which lets
        ``get_documents_pending_convert()`` use ``txt_path IS NULL`` as a
        reliable "needs conversion" marker.

        ``site_id`` defaults to ``self.site_id`` when omitted.
        """
        self._check_cancelled()
        doc_dict.setdefault("site_id", self.site_id)
        return db_module.upsert_document(self._conn, doc_dict)

    # ------------------------------------------------------------------
    # libertree v2 — INSERT into the new ``data/libertree.db`` schema
    # ------------------------------------------------------------------

    def _save_paper_v2(self, doc):
        """Insert into ``data/libertree.db``'s ``documents`` table.

        ``doc`` keys map directly to documents columns (except ``seq_id``,
        which is auto-assigned). ``site_id`` defaults to ``self.site_id``.
        Required: ``meta_url``, ``title``.

        Returns the assigned (or pre-existing dedup-matched) ``seq_id``.

        This bypasses the legacy ``papers``/``documents`` tables entirely.
        Called from :meth:`_save_paper` when the bound connection points
        at libertree.db. Callers can also invoke it directly when the
        crawler explicitly opens a libertree connection.
        """
        self._check_cancelled()
        from . import db_backend
        if not isinstance(doc, dict):
            raise TypeError("doc must be a dict")
        doc.setdefault("site_id", self.site_id)
        backend = db_backend.get_backend()
        existing = backend.find_by_dedup_key(
            self._conn, doc["site_id"], doc.get("post_number"), doc.get("meta_url")
        )
        if self.delivery_mode == "incremental" and existing is not None:
            self._last_save_created = False
            self._consecutive_known += 1
            if (self.DELIVERY_ORDER == "newest_first"
                    and self._consecutive_known >= max(1, self.UP_TO_DATE_THRESHOLD)):
                raise CrawlUpToDate(
                    f"{self._consecutive_known} consecutive known documents")
            return existing
        seq_id = backend.insert_document(self._conn, doc)
        self._last_save_created = existing is None
        if existing is None:
            self._consecutive_known = 0
        return seq_id

    def _conn_is_libertree(self):
        """Return True if the bound DB connection looks like libertree.db.

        Detection is best-effort: we look for the ``documents.seq_id``
        column (libertree-only) and the absence of ``papers`` (legacy-only).
        """
        try:
            cur = self._conn.execute("PRAGMA table_info(documents)")
            cols = {r[1] for r in cur.fetchall()}
            if "seq_id" in cols:
                return True
        except Exception:
            return False
        return False

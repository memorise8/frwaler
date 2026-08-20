# -*- coding: utf-8 -*-
"""ScienceON ARTI metadata crawlers for Libertree Delivery.

This single-file crawler contains the KISTI API authentication/transport
adapter and two independently schedulable crawlers:

* ``scienceon-api``: Korean papers and proceedings.
* ``scienceon-foreign-api``: foreign-paper metadata only.

Large historical collection uses Delivery's flat resume cursor.  A cursor
always points to the next CN-prefix subtree to inspect; it advances only after
the current terminal slice reaches the API-reported completeness threshold.
PDF bytes are deliberately out of scope here.  ``pdf_url`` is retained when
the ARTI response exposes one so a backend-neutral downloader can process it
later.
"""
from __future__ import annotations

import base64
import json
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lxml import etree

from crawler.base_crawler import BaseCrawler


TOKEN_URL = "https://apigateway.kisti.re.kr/tokenrequest.do"
CALL_URL = "https://apigateway.kisti.re.kr/openapicall.do"
SCIENCEON_URL = "https://scienceon.kisti.re.kr"
AES_IV = b"jvHJ1EFA0IXBrxxz"


class ScienceOnError(RuntimeError):
    """Content-free operational error safe to retain in a crawl job log."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _default_mac() -> str:
    node = uuid.getnode()
    return "-".join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


class ScienceOnClient:
    """Small authenticated client for the ScienceON ARTI XML API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client_id: str | None = None,
        mac_address: str | None = None,
        token_cache: str | Path | None = None,
        session: requests.Session | None = None,
        delay: float = 1.0,
        should_cancel: Callable[[], None] | None = None,
    ):
        self.api_key = api_key or os.environ.get("SCIENCEON_KEY", "")
        self.client_id = client_id or os.environ.get("SCIENCEON_CLIENT_ID", "")
        self.mac_address = (
            mac_address or os.environ.get("SCIENCEON_MAC_ADDRESS") or _default_mac()
        )
        if len(self.api_key) != 32:
            raise ValueError("SCIENCEON_KEY must be a 32-character authentication key")
        if not self.client_id:
            raise ValueError("SCIENCEON_CLIENT_ID is required")
        cache_default = os.environ.get(
            "SCIENCEON_TOKEN_CACHE", "/data/runtime/scienceon_token.json"
        )
        self.token_cache = Path(token_cache or cache_default)
        self.session = session or requests.Session()
        self.delay = max(0.0, float(delay))
        self.base_delay = self.delay
        self.should_cancel = should_cancel or (lambda: None)
        self._tokens: dict | None = None

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.should_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _accounts(self) -> str:
        payload = json.dumps(
            {
                "datetime": datetime.now().strftime("%Y%m%d%H%M%S"),
                "mac_address": self.mac_address,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        padder = padding.PKCS7(128).padder()
        padded = padder.update(payload) + padder.finalize()
        encryptor = Cipher(
            algorithms.AES(self.api_key.encode("utf-8")), modes.CBC(AES_IV)
        ).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.urlsafe_b64encode(encrypted).decode("ascii")

    @staticmethod
    def _expired(value: str | None, margin_seconds: int = 120) -> bool:
        if not value:
            return True
        try:
            expires_at = datetime.strptime(value.split(".")[0], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return True
        return (expires_at - datetime.now()).total_seconds() < margin_seconds

    def _read_cache(self) -> dict | None:
        try:
            value = json.loads(self.token_cache.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, tokens: dict) -> None:
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(tokens), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.token_cache)

    @staticmethod
    def _parse_token(response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ScienceOnError("token endpoint returned an invalid response") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            code = payload.get("errorCode") if isinstance(payload, dict) else None
            raise ScienceOnError("token issuance was rejected", code=code)
        return payload

    def _issue(self) -> dict:
        self.should_cancel()
        try:
            response = self.session.get(
                TOKEN_URL,
                params={"accounts": self._accounts(), "client_id": self.client_id},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ScienceOnError("token endpoint is unavailable") from exc
        return self._parse_token(response)

    def _refresh(self, refresh_token: str) -> dict:
        self.should_cancel()
        try:
            response = self.session.get(
                TOKEN_URL,
                params={"refresh_token": refresh_token, "client_id": self.client_id},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ScienceOnError("token endpoint is unavailable") from exc
        tokens = self._parse_token(response)
        tokens.setdefault("refresh_token", refresh_token)
        return tokens

    def access_token(self, *, force_new: bool = False) -> str:
        tokens = None if force_new else (self._tokens or self._read_cache())
        if (
            tokens
            and tokens.get("access_token")
            and not self._expired(tokens.get("access_token_expire"))
        ):
            self._tokens = tokens
            return str(tokens["access_token"])
        if (
            tokens
            and tokens.get("refresh_token")
            and not self._expired(tokens.get("refresh_token_expire"))
        ):
            try:
                tokens = self._refresh(str(tokens["refresh_token"]))
            except ScienceOnError:
                tokens = None
        if tokens is None:
            tokens = self._issue()
        self._tokens = tokens
        self._write_cache(tokens)
        return str(tokens["access_token"])

    def call(self, *, action: str, target: str, retries: int = 5, **params):
        last_error: ScienceOnError | None = None
        force_new = False
        for attempt in range(retries):
            self._wait(self.delay)
            try:
                token = self.access_token(force_new=force_new)
            except ScienceOnError as exc:
                last_error = exc
                continue
            force_new = False
            query = {
                "client_id": self.client_id,
                "token": token,
                "version": "1.0",
                "action": action,
                "target": target,
                **{key: value for key, value in params.items() if value is not None},
            }
            self.should_cancel()
            try:
                response = self.session.get(CALL_URL, params=query, timeout=60)
            except requests.RequestException as exc:
                last_error = ScienceOnError("ScienceON API is unavailable")
                last_error.__cause__ = exc
                continue
            try:
                root = etree.fromstring(response.content)
            except etree.XMLSyntaxError as exc:
                last_error = ScienceOnError("ScienceON API returned invalid XML")
                last_error.__cause__ = exc
                continue
            error_code = root.findtext(".//errorCode")
            if error_code:
                api_error = ScienceOnError("ScienceON API rejected the request", code=error_code)
                if error_code == "E4103":
                    force_new = True
                    last_error = api_error
                    continue
                if error_code == "E4290":
                    self.delay = min(max(self.delay, 0.2) * 1.25, 5.0)
                    self._wait((attempt + 1) * 15)
                    last_error = api_error
                    continue
                raise api_error
            if self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay * 0.995)
            return root
        raise last_error or ScienceOnError("ScienceON request failed")

    def search(
        self,
        query: dict,
        *,
        page: int = 1,
        row_count: int = 100,
        sort_field: str | None = None,
    ) -> tuple[int, list[dict]]:
        root = self.call(
            action="search",
            target="ARTI",
            searchQuery=json.dumps(query, ensure_ascii=False),
            curPage=page,
            rowCount=row_count,
            sortField=sort_field,
        )
        total_text = root.findtext(".//TotalCount")
        try:
            total = int(total_text) if total_text is not None else None
        except ValueError as exc:
            raise ScienceOnError("ScienceON returned an invalid total count") from exc
        if total is None:
            raise ScienceOnError("ScienceON response omitted the total count")
        records: list[dict] = []
        for element in root.iter("record"):
            record: dict = {}
            for item in element.iter("item"):
                key = item.get("metaCode") or item.get("name")
                if key:
                    record[key] = (item.text or "").strip() or None
            if not record:
                for child in element:
                    record[etree.QName(child).localname] = (
                        (child.text or "").strip() or None
                    )
            if record:
                records.append(record)
        return total, records

    def iter_search(
        self, query: dict, *, sort_field: str | None = None
    ) -> Iterator[dict]:
        page = 1
        yielded = 0
        empty_attempts = 0
        while True:
            total, records = self.search(query, page=page, sort_field=sort_field)
            if not records:
                if yielded >= total:
                    return
                empty_attempts += 1
                if empty_attempts >= 3:
                    raise ScienceOnError("ScienceON returned an unexpected empty page")
                self._wait(empty_attempts * 5)
                continue
            empty_attempts = 0
            for record in records:
                yield record
                yielded += 1
            if yielded >= total:
                return
            if (page + 1) * 100 >= 10_000:
                return
            page += 1


class _ScienceOnCrawlerBase(BaseCrawler):
    """Shared metadata mapping and deterministic CN-prefix traversal."""

    DELIVERY_ORDER = "arbitrary"
    MAX_SLICE = 8_000
    MAX_PREFIX_LENGTH = 19
    COMPLETENESS = 0.99
    SPLIT_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    SORT_FIELDS = ("pubyear", "title", "jtitle", None)
    ROOTS: tuple[str, ...] = ()
    ROOT_DBCODES: dict[str, frozenset[str] | None] = {}
    ALLOW_UNKNOWN_DBCODE = True

    def __init__(
        self,
        db_conn,
        delay=1.0,
        client: ScienceOnClient | None = None,
    ):
        super().__init__(db_conn, delay=delay)
        self._client = client or ScienceOnClient(
            session=self._session,
            delay=delay,
            should_cancel=self._check_cancelled,
        )

    def _allowed(self, root: str, record: dict) -> bool:
        allowed = self.ROOT_DBCODES.get(root)
        if allowed is None:
            return True
        dbcode = (record.get("DBCode") or "").strip().upper()
        return (not dbcode and self.ALLOW_UNKNOWN_DBCODE) or dbcode in allowed

    @staticmethod
    def _document(record: dict) -> dict | None:
        cn = (record.get("CN") or "").strip()
        if not cn:
            return None
        meta_url = record.get("ContentURL") or (
            f"{SCIENCEON_URL}/srch/selectPORSrchArticle.do?cn={cn}"
        )
        keywords = record.get("Keyword") or record.get("Keyword2")
        doi = (record.get("DOI") or "").strip()
        if doi:
            keywords = f"{keywords}; DOI:{doi}" if keywords else f"DOI:{doi}"
        fulltext_flag = (record.get("FulltextFlag") or "0").strip().upper()
        fulltext_url = record.get("FulltextURL")
        return {
            "external_id": cn,
            "meta_url": meta_url,
            "title": record.get("Title") or record.get("Title2") or "(untitled)",
            "published_date": record.get("Pubdate") or record.get("Pubyear"),
            "authors": record.get("Author") or record.get("Author2"),
            "publisher": record.get("Publisher"),
            "journal": record.get("JournalName"),
            "pdf_url": (
                fulltext_url
                if fulltext_url and fulltext_flag not in ("", "0", "N")
                else None
            ),
            "keywords": keywords,
            "abstract": record.get("Abstract") or record.get("Abstract2"),
        }

    def _save_record(self, root: str, record: dict) -> bool:
        if not self._allowed(root, record):
            return False
        document = self._document(record)
        if document is None:
            return False
        self._save_paper(document)
        return bool(self._last_save_created)

    def _count(self, prefix: str, *, year: int | None = None) -> int:
        query = {"CN": prefix + "*"}
        if year is not None:
            query["PY"] = str(year)
        total, _ = self._client.search(query, row_count=1)
        return total

    def _successor(self, root: str, prefix: str) -> str | None:
        if prefix == root:
            return None
        suffix = list(prefix[len(root) :])
        while suffix:
            try:
                index = self.SPLIT_CHARS.index(suffix[-1])
            except ValueError as exc:
                raise ScienceOnError("cursor contains an unsupported prefix") from exc
            if index + 1 < len(self.SPLIT_CHARS):
                suffix[-1] = self.SPLIT_CHARS[index + 1]
                return root + "".join(suffix)
            suffix.pop()
        return None

    def _terminal(self, root: str, candidate: str) -> tuple[str | None, int]:
        """Return the next non-empty terminal slice at/after candidate."""
        current: str | None = candidate
        while current is not None:
            self._check_cancelled()
            total = self._count(current)
            if total == 0:
                current = self._successor(root, current)
                continue
            if total <= self.MAX_SLICE:
                return current, total
            if len(current) >= self.MAX_PREFIX_LENGTH:
                raise ScienceOnError("ScienceON prefix cannot be split below the reach cap")
            current += self.SPLIT_CHARS[0]
        return None, 0

    def _collect_terminal(self, root: str, prefix: str, total: int) -> tuple[int, int]:
        seen: set[str] = set()
        saved = 0
        successful_passes = 0
        query = {"CN": prefix + "*"}
        for sort_field in self.SORT_FIELDS:
            try:
                for record in self._client.iter_search(query, sort_field=sort_field):
                    self._check_cancelled()
                    cn = (record.get("CN") or "").strip()
                    if not cn or cn in seen:
                        continue
                    seen.add(cn)
                    if self._save_record(root, record):
                        saved += 1
                successful_passes += 1
            except ScienceOnError:
                continue
            if successful_passes > 1 and len(seen) >= total:
                break
        total_now = self._count(prefix) if len(seen) < total else total
        required = max(1, math.ceil(total_now * self.COMPLETENESS))
        if not successful_passes or len(seen) < required:
            raise ScienceOnError(
                f"ScienceON slice incomplete ({len(seen)}/{total_now})"
            )
        print(
            f"[{self.site_id}] {prefix}* complete: "
            f"fetched={len(seen):,} new={saved:,}",
            flush=True,
        )
        return len(seen), saved

    def _crawl_backfill(self, limit: int | None) -> None:
        cursor = self.delivery_cursor or {}
        try:
            root_index = int(cursor.get("root_index", 0))
        except (TypeError, ValueError):
            root_index = 0
        if root_index < 0 or root_index > len(self.ROOTS):
            raise ScienceOnError("invalid ScienceON backfill cursor")
        candidate = str(cursor.get("prefix") or "")
        slice_budget = max(1, int(os.environ.get("SCIENCEON_SLICE_BUDGET", "4")))
        completed_slices = 0
        saved_total = 0

        while root_index < len(self.ROOTS):
            root = self.ROOTS[root_index]
            if not candidate.startswith(root):
                candidate = root
            terminal, total = self._terminal(root, candidate)
            if terminal is None:
                root_index += 1
                candidate = self.ROOTS[root_index] if root_index < len(self.ROOTS) else ""
                self._advance_cursor(
                    {"root_index": root_index, "prefix": candidate}, 0
                )
                continue

            fetched, saved = self._collect_terminal(root, terminal, total)
            saved_total += saved
            completed_slices += 1
            successor = self._successor(root, terminal)
            if successor is None:
                root_index += 1
                candidate = self.ROOTS[root_index] if root_index < len(self.ROOTS) else ""
            else:
                candidate = successor
            self._advance_cursor(
                {"root_index": root_index, "prefix": candidate}, fetched
            )
            if completed_slices >= slice_budget or (
                limit is not None and saved_total >= limit
            ):
                return

        self._mark_exhausted()

    def _crawl_incremental(self, limit: int | None) -> None:
        """Bounded rolling reconciliation; historical completeness is backfill's job."""
        years = max(1, int(os.environ.get("SCIENCEON_INCREMENTAL_YEARS", "2")))
        pages = max(1, int(os.environ.get("SCIENCEON_INCREMENTAL_PAGES", "3")))
        current_year = datetime.now().year
        saved = 0
        for year in range(current_year, current_year - years, -1):
            for root in self.ROOTS:
                query = {"CN": root + "*", "PY": str(year)}
                for page in range(1, pages + 1):
                    self._check_cancelled()
                    total, records = self._client.search(
                        query, page=page, sort_field="pubyear"
                    )
                    if not records:
                        if (page - 1) * 100 < total:
                            raise ScienceOnError(
                                "ScienceON returned an unexpected empty incremental page"
                            )
                        break
                    for record in records:
                        if self._save_record(root, record):
                            saved += 1
                            if limit is not None and saved >= limit:
                                return
                    if page * 100 >= total:
                        break

    def crawl(self, limit=None):
        if limit is not None and int(limit) <= 0:
            raise ValueError("limit must be positive")
        if self.delivery_mode == "backfill":
            self._crawl_backfill(limit)
            return
        if self.delivery_mode == "full":
            raise ValueError("ScienceON full collection must use mode=backfill")
        self._crawl_incremental(limit)


class ScienceOnDomesticCrawler(_ScienceOnCrawlerBase):
    site_id = "scienceon-api"
    site_name = "ScienceON 국내 논문"
    base_url = "https://apigateway.kisti.re.kr"
    ROOTS = ("JAKO", "DIKO", "NPAP", "ATN", "ART")
    ROOT_DBCODES = {
        "JAKO": frozenset({"JAKO"}),
        "DIKO": frozenset({"DIKO"}),
        "NPAP": frozenset({"CFKO"}),
        "ATN": frozenset({"JAKO"}),
        "ART": frozenset({"JAKO"}),
    }


class ScienceOnForeignCrawler(_ScienceOnCrawlerBase):
    site_id = "scienceon-foreign-api"
    site_name = "ScienceON 외국 논문 메타데이터"
    base_url = "https://apigateway.kisti.re.kr"
    ALLOW_UNKNOWN_DBCODE = False
    ROOTS = ("NART", "PRE", "NPAP", "ATN")
    ROOT_DBCODES = {
        "NART": None,
        "PRE": None,
        "NPAP": frozenset({"CFFO"}),
        "ATN": frozenset({"JAFO"}),
    }

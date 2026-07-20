# -*- coding: utf-8 -*-
"""Crawler for salute.gov.it/new publications (Rapporti)."""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import datetime

from crawler.base_crawler import BaseCrawler


class SaluteGovItNewCrawler(BaseCrawler):
    site_id = "salute-gov-it-new"
    site_name = "Ministero della Salute - Pubblicazioni (new)"
    base_url = "https://www.salute.gov.it/new"

    _PORTAL_PAGE = "https://www.salute.gov.it/new/it/sezione/pubblicazioni/"
    _PAGE_DATA_URL = "https://www.salute.gov.it/new/page-data/it/sezione/pubblicazioni/page-data.json"
    _SHIELD_AID = "487d9af2a1888c7641f11ee196aa7161"
    _TARGET_TYPE = "Rapporti"

    def crawl(self, limit=None):
        run_id = uuid.uuid4().hex
        cookie_file = f"/tmp/salute_cookies_{run_id}.txt"
        data_file = f"/tmp/salute_page_data_{run_id}.json"
        try:
            nodes = self._fetch_nodes(cookie_file, data_file)
        finally:
            for f in (cookie_file, data_file):
                try:
                    os.unlink(f)
                except OSError:
                    pass

        saved = 0
        for node in nodes:
            if limit is not None and saved >= limit:
                break
            paper = self._node_to_paper(node)
            if paper is None:
                continue
            self._save_paper(paper)
            saved += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] saved {saved} records")
        return saved

    # ------------------------------------------------------------------

    def _fetch_nodes(self, cookie_file, data_file):
        ua = self.USER_AGENT

        # Step 1: warm cookies
        r1 = subprocess.run(
            [
                "curl", "-s", "-L",
                "-c", cookie_file, "-b", cookie_file,
                "-A", ua,
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H", "Accept-Language: it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                self._PORTAL_PAGE,
            ],
            capture_output=True, timeout=60,
        )
        if r1.returncode != 0:
            raise RuntimeError(f"[{self.site_id}] Cookie fetch failed: {r1.stderr.decode()}")

        # Step 2: fetch page-data.json
        r2 = subprocess.run(
            [
                "curl", "-s",
                "-b", cookie_file,
                "-A", ua,
                "-H", f"X-SHIELD-AID: {self._SHIELD_AID}",
                "-H", "Sec-Fetch-Dest: empty",
                "-H", "Sec-Fetch-Mode: cors",
                "-H", "Sec-Fetch-Site: same-origin",
                "-H", f"Referer: {self._PORTAL_PAGE}",
                "-H", "Accept: */*",
                "-o", data_file,
                self._PAGE_DATA_URL,
            ],
            capture_output=True, timeout=120,
        )
        if r2.returncode != 0:
            raise RuntimeError(f"[{self.site_id}] page-data fetch failed: {r2.stderr.decode()}")

        with open(data_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        try:
            nodes = (
                payload["result"]["data"]["node"]
                ["relationships"]["field_view_list"][0]
                ["fields"]["view"]["view_query_data"]["nodes"]
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"[{self.site_id}] Unexpected page-data structure: {exc}")

        rapporti = [
            n for n in nodes
            if self._node_type(n) == self._TARGET_TYPE
        ]
        print(f"[{self.site_id}] {len(rapporti)} {self._TARGET_TYPE} found (of {len(nodes)} total)")
        return rapporti

    @staticmethod
    def _node_type(node):
        try:
            return node["relationships"]["field_tipologia_pubblicazione"]["name"]
        except (KeyError, TypeError):
            return None

    def _node_to_paper(self, node):
        drupal_id = node.get("drupal_id") or node.get("id")
        title = (node.get("title") or "").strip() or None

        raw_date = node.get("field_data_pubblicazione") or ""
        published_date = self._parse_date(raw_date)

        path_alias = (node.get("path") or {}).get("alias") or ""
        meta_url = (self.base_url + path_alias) if path_alias else self.base_url

        # themes → keywords
        themes = []
        for t in (node.get("relationships") or {}).get("field_temi") or []:
            name = (t or {}).get("name")
            if name:
                themes.append(name)
        keywords = ", ".join(themes) if themes else None

        # pdf_url
        pdf_url = None
        files = (node.get("relationships") or {}).get("field_listafile") or []
        if files:
            first_url = (files[0] or {}).get("url")
            if first_url:
                pdf_url = self.base_url + first_url if first_url.startswith("/") else first_url

        # abstract
        abstract = self._build_abstract(node, self._TARGET_TYPE, published_date, themes)
        if abstract is None or len(abstract) < 100:
            return None

        return {
            "site_id": self.site_id,
            "external_id": str(drupal_id) if drupal_id else None,
            "title": title,
            "authors": None,
            "abstract": abstract,
            "keywords": keywords,
            "published_date": published_date,
            "url": meta_url,
            "pdf_url": pdf_url,
            "department": "Ministero della Salute",
            "metadata": json.dumps({
                "tipologia": self._TARGET_TYPE,
                "temi": themes,
            }, ensure_ascii=False),
        }

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw.strip() or None

    @staticmethod
    def _strip_html(html):
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _build_abstract(self, node, doc_type, pub_date, themes):
        body_html = ((node.get("body") or {}).get("processed") or "").strip()
        if body_html:
            cleaned = self._strip_html(body_html)
            if len(cleaned) >= 100:
                return cleaned

        parts = [f"Tipo: {doc_type}."]
        if pub_date:
            parts.append(f"Data: {pub_date}.")
        if themes:
            parts.append(f"Temi: {', '.join(themes)}.")
        candidate = " ".join(parts)
        return candidate if len(candidate) >= 100 else None

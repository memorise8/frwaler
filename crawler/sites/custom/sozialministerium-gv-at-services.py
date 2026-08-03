# -*- coding: utf-8 -*-
"""Crawler for sozialministerium.gv.at Services/Studien.

The Studien page is the real list endpoint. It is static HTML, with one
Art. 20 table and several collapsible sections linking to PDFs, external
repository detail pages, and brochure-service download endpoints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from email.message import Message
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "sozialministerium-gv-at-services"
_BASE_URL = "https://www.sozialministerium.gv.at"
_START_URL = "https://www.sozialministerium.gv.at/Services/Studien.html"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 50
_ABSTRACT_TARGET_CHARS = 100
_PDF_ABSTRACT_CHARS = 1800
_MINISTRY = (
    "Bundesministerium für Arbeit, Soziales, Gesundheit, Pflege und "
    "Konsumentenschutz"
)

_DE_MONTHS = {
    "jänner": "01",
    "jaenner": "01",
    "januar": "01",
    "februar": "02",
    "märz": "03",
    "maerz": "03",
    "april": "04",
    "mai": "05",
    "juni": "06",
    "juli": "07",
    "august": "08",
    "september": "09",
    "oktober": "10",
    "november": "11",
    "dezember": "12",
}


class SozialministeriumGvAtServicesCrawler(BaseCrawler):
    site_id = "sozialministerium-gv-at-services"
    site_name = "Custom: sozialministerium-gv-at-services"
    base_url = "https://www.sozialministerium.gv.at"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 15,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for "
                    f"{url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _download_pdf(self, url, *, referer=None, timeout=90):
        """Download a PDF-like URL to a temp file and return (path, headers)."""
        tmp = tempfile.NamedTemporaryFile(prefix=f"{_SITE_ID}-", suffix=".pdf", delete=False)
        tmp_path = tmp.name
        tmp.close()

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-D",
            "-",
            "-o",
            tmp_path,
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 20,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                headers = result.stdout.decode("utf-8", errors="replace")
                size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                if result.returncode == 0 and size > 512:
                    return tmp_path, headers
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} size={size} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] PDF download attempt {attempt}/{len(waits)} "
                    f"failed for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        print(f"[{_SITE_ID}] PDF download failed after 3 attempts for {url}: {last_error}")
        return None, ""

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        html = raw or ""
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
            return None

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        text = str(value).replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @classmethod
    def _link_text(cls, link):
        try:
            from bs4 import BeautifulSoup
            clone = BeautifulSoup(str(link), "html.parser")
            for node in clone.select(".fileinfo, .icon"):
                node.decompose()
            text = clone.get_text(" ", strip=True)
        except Exception:
            text = link.get_text(" ", strip=True) if link else ""
        return cls._one_line(text)

    @classmethod
    def _node_text_without_links(cls, node):
        try:
            from bs4 import BeautifulSoup
            clone = BeautifulSoup(str(node), "html.parser")
            for tag in clone.find_all("a"):
                tag.decompose()
            for tag in clone.select(".fileinfo, .icon"):
                tag.decompose()
            return cls._one_line(clone.get_text(" ", strip=True))
        except Exception:
            return ""

    @classmethod
    def _prefix_before_anchor(cls, anchor):
        parent = anchor.find_parent(["li", "p", "td"])
        if parent is None:
            return ""
        parts = []
        for child in parent.children:
            if child is anchor:
                break
            if getattr(child, "name", None) == "a":
                continue
            text = child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child)
            text = cls._one_line(text)
            if text:
                parts.append(text)
        prefix = cls._one_line(" ".join(parts))
        return prefix.strip(" -–:\u00a0")

    @staticmethod
    def _absolute_url(href, base=_START_URL):
        if not href:
            return ""
        return urljoin(base, href.strip())

    @staticmethod
    def _url_key(url):
        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        query = parsed.query
        return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")

    @staticmethod
    def _looks_like_pdf_or_download(url):
        lower = url.lower()
        parsed = urlparse(url)
        return (
            ".pdf" in lower
            or "/home/download" in parsed.path.lower()
            or "attachmentname=" in parsed.query.lower()
        )

    @staticmethod
    def _is_probable_record_link(url):
        parsed = urlparse(url)
        lower = url.lower()
        if not parsed.scheme.startswith("http"):
            return False
        if ".pdf" in lower or "/home/download" in parsed.path.lower():
            return True
        if "jasmin.goeg.at/id/eprint/" in lower:
            return True
        if "goeg.at/" in lower and "creativecommons.org" not in lower:
            return True
        if "gesundheit.gv.at/" in lower:
            return True
        return False

    @staticmethod
    def _filename_from_url(url):
        parsed = urlparse(url or "")
        qs = parse_qs(parsed.query)
        for key in ("attachmentName", "filename", "file"):
            values = qs.get(key)
            if values and values[0]:
                return unquote_plus(values[0].split("/")[-1])
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if tail and "." in tail:
            return unquote(tail)
        return None

    @staticmethod
    def _filename_from_headers(headers):
        candidates = re.findall(r"(?im)^content-disposition:\s*(.+)$", headers or "")
        for raw in reversed(candidates):
            msg = Message()
            msg["content-disposition"] = raw
            filename = msg.get_filename()
            if filename:
                return unquote_plus(filename)
            m = re.search(r"filename\*=UTF-8''([^;]+)", raw, re.I)
            if m:
                return unquote(m.group(1))
            m = re.search(r'filename="?([^";]+)"?', raw, re.I)
            if m:
                return unquote_plus(m.group(1))
        return None

    @staticmethod
    def _extract_native_ids(url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        meta = {}
        post_number = None

        publication_id = (qs.get("publicationId") or [None])[0]
        if publication_id:
            meta["publicationId"] = publication_id
            post_number = publication_id if publication_id.isdigit() else publication_id

        m = re.search(r"/id/eprint/(\d+)", parsed.path)
        if m:
            meta["eprint_id"] = m.group(1)
            post_number = m.group(1)

        m = re.search(r"/dam/jcr:([^/]+)", parsed.path)
        if m:
            meta["jcr_id"] = m.group(1)
            if not post_number:
                post_number = m.group(1)

        if not post_number:
            filename = SozialministeriumGvAtServicesCrawler._filename_from_url(url)
            if filename:
                post_number = re.sub(r"\.[A-Za-z0-9]{2,6}$", "", filename)
            else:
                post_number = parsed.path.strip("/") or url

        external_id = post_number or SozialministeriumGvAtServicesCrawler._url_key(url)
        return str(external_id), str(post_number) if post_number else None, meta

    @staticmethod
    def _split_date_values(raw):
        raw = SozialministeriumGvAtServicesCrawler._one_line(raw)
        if not raw:
            return []
        parts = [p.strip() for p in re.split(r"\s*[,;]\s*", raw) if p.strip()]
        return parts if parts else [raw]

    @staticmethod
    def _parse_date(raw):
        text = SozialministeriumGvAtServicesCrawler._one_line(raw).lower()
        if not text:
            return None

        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
        if m:
            day, month, year = m.groups()
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

        m = re.search(
            r"\b(\d{1,2})\.\s*(jänner|jaenner|januar|februar|märz|maerz|april|"
            r"mai|juni|juli|august|september|oktober|november|dezember)\s+(\d{4})\b",
            text,
        )
        if m:
            day, month_name, year = m.groups()
            month = _DE_MONTHS.get(month_name)
            if month:
                return f"{year}-{month}-{day.zfill(2)}"

        m = re.search(
            r"\b(jänner|jaenner|januar|februar|märz|maerz|april|mai|juni|juli|"
            r"august|september|oktober|november|dezember)\s+(\d{4})\b",
            text,
        )
        if m:
            month_name, year = m.groups()
            month = _DE_MONTHS.get(month_name)
            if month:
                return f"{year}-{month}-01"

        m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    @staticmethod
    def _parse_pdf_date(raw):
        if not raw:
            return None
        m = re.search(r"D:(\d{4})(\d{2})(\d{2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        months = {
            "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
            "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
            "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
        }
        m = re.search(r"(\w{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+(\d{4})", raw)
        if m:
            month = months.get(m.group(1))
            if month:
                return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"
        m = re.search(r"\b(20\d{2}|19\d{2})\b", raw)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html, page_url):
        soup = self._make_soup(html)
        if soup is None:
            return [], False

        content = soup.find(id="content") or soup.find("main") or soup
        records = []

        official_heading = "Veröffentlichungen von Studien, Gutachten und Umfragen gemäß Art. 20 Abs. 5 B-VG"
        table = content.find("table", class_="table") or content.find("table")
        if table is not None:
            records.extend(self._parse_table_records(table, official_heading, page_url))

        for group in content.select("div.collapsible-group"):
            group_title_node = group.find(["h2", "h3", "h4"])
            group_title = self._one_line(group_title_node.get_text(" ", strip=True)) if group_title_node else ""
            if "weiterführende information" in group_title.lower():
                continue
            records.extend(self._parse_group_records(group, group_title, page_url))

        has_next = bool(
            content.select_one('a[rel="next"], .pagination .next a, a.next')
            or content.find("a", string=re.compile(r"Weiter|Nächste|Next", re.I))
        )
        return records, has_next

    def _parse_table_records(self, table, category, page_url):
        records = []
        for row_index, tr in enumerate(table.find_all("tr")):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 2:
                continue

            title_cell = cells[0]
            raw_date = self._one_line(cells[1].get_text(" ", strip=True))
            cost_raw = self._one_line(cells[2].get_text(" ", strip=True)) if len(cells) > 2 else ""
            date_values = self._split_date_values(raw_date)
            group_label = self._node_text_without_links(title_cell)
            row_text = self._one_line(title_cell.get_text(" ", strip=True))

            links = [a for a in title_cell.find_all("a", href=True)]
            for link_index, link in enumerate(links):
                href = link.get("href", "").strip()
                url = self._absolute_url(href, page_url)
                if not self._is_probable_record_link(url):
                    continue

                link_title = self._link_text(link)
                if not link_title:
                    continue
                title = link_title
                if group_label and group_label.lower() not in link_title.lower():
                    title = f"{group_label}: {link_title}"

                item_date_raw = raw_date
                if len(date_values) == len(links):
                    item_date_raw = date_values[link_index]
                listed_date = self._parse_date(item_date_raw)

                records.append({
                    "url": url,
                    "title": title,
                    "list_title": link_title,
                    "group_label": group_label,
                    "category": category,
                    "raw_date": item_date_raw,
                    "row_date_raw": raw_date,
                    "listed_date": listed_date,
                    "published_date": listed_date,
                    "cost_raw": cost_raw,
                    "row_index": row_index,
                    "link_index": link_index,
                    "row_text": row_text,
                    "source": "art_20_table",
                })
        return records

    def _parse_group_records(self, group, group_title, page_url):
        records = []
        for link_index, link in enumerate(group.find_all("a", href=True)):
            href = link.get("href", "").strip()
            url = self._absolute_url(href, page_url)
            if not self._is_probable_record_link(url):
                continue

            link_title = self._link_text(link)
            if not link_title:
                continue

            heading = self._nearest_heading(link, group)
            prefix = self._prefix_before_anchor(link)
            title = link_title
            volume = None
            if prefix:
                m = re.search(r"\bBand\s+(\d+)\b", prefix, re.I)
                if m:
                    volume = m.group(1)
                    title = f"{prefix}: {link_title}"
                elif len(link_title) < 45 and prefix.lower() not in link_title.lower():
                    title = f"{prefix}: {link_title}"

            context_node = link.find_parent(["li", "p"]) or link
            row_text = self._one_line(context_node.get_text(" ", strip=True))
            category_parts = [p for p in (group_title, heading) if p]
            category = " / ".join(dict.fromkeys(category_parts))
            listed_date = self._parse_date(row_text) or self._parse_date(link_title) or self._parse_date(url)

            records.append({
                "url": url,
                "title": title,
                "list_title": link_title,
                "group_label": prefix,
                "category": category or group_title or "Studien",
                "raw_date": None,
                "row_date_raw": None,
                "listed_date": listed_date,
                "published_date": listed_date,
                "cost_raw": None,
                "row_index": None,
                "link_index": link_index,
                "row_text": row_text,
                "source": "collapsible_group",
                "series": group_title if "Studienreihe" in group_title else None,
                "volume": volume,
            })
        return records

    def _nearest_heading(self, node, boundary):
        headings = []
        for heading in boundary.find_all(["h2", "h3", "h4"]):
            if heading is node:
                continue
            if self._appears_before(heading, node):
                text = self._one_line(heading.get_text(" ", strip=True))
                if text:
                    headings.append(text)
        return headings[-1] if headings else ""

    @staticmethod
    def _appears_before(left, right):
        for element in left.next_elements:
            if element is right:
                return True
        return False

    # ------------------------------------------------------------------
    # Detail/PDF parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, item):
        url = item["url"]
        if self._looks_like_pdf_or_download(url):
            return self._parse_pdf_detail(url, item)

        html = self._curl_text(url, referer=_START_URL, timeout=45)
        if not html:
            raise RuntimeError(f"empty detail response for {url}")
        soup = self._make_soup(html)
        if soup is None:
            raise RuntimeError("detail HTML parse failed")

        detail = {
            "title": item.get("title"),
            "abstract": "",
            "authors": None,
            "publisher": _MINISTRY,
            "published_date": item.get("published_date"),
            "listed_date": item.get("listed_date"),
            "pdf_url": None,
            "original_filename": None,
            "journal": None,
            "doi": None,
            "metadata": {},
        }

        h1 = soup.find("h1")
        if h1:
            title = self._one_line(h1.get_text(" ", strip=True))
            if title and len(title) > 5:
                detail["title"] = title
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = self._one_line(og_title.get("content"))
            if title and len(title) > 5:
                detail["title"] = title

        abstracts = []
        for selector in (
            'meta[name="description"]',
            'meta[property="og:description"]',
            ".abstract",
            "#abstract",
            "section.abstract",
            "div.abstract",
        ):
            node = soup.select_one(selector)
            if not node:
                continue
            text = node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
            text = self._one_line(text)
            if len(text) >= _ABSTRACT_MIN_CHARS:
                abstracts.append(text)

        content = soup.find("main") or soup.find(id="content") or soup.find("body") or soup
        for p in content.find_all("p"):
            text = self._one_line(p.get_text(" ", strip=True))
            if len(text) >= 80 and text not in abstracts:
                abstracts.append(text)
            if len(" ".join(abstracts)) >= _PDF_ABSTRACT_CHARS:
                break
        detail["abstract"] = " ".join(abstracts)[:_PDF_ABSTRACT_CHARS]

        authors = [m.get("content", "").strip() for m in soup.find_all("meta", attrs={"name": "citation_author"})]
        authors = [a for a in authors if a]
        if authors:
            detail["authors"] = "; ".join(authors)
        else:
            citation = abstracts[0] if abstracts else ""
            m = re.match(r"(.+?)\s+\((?:19|20)\d{2}\):", citation)
            if m:
                raw_authors = m.group(1)
                pieces = [self._one_line(p) for p in re.split(r"\s*;\s*", raw_authors) if p.strip()]
                if pieces:
                    detail["authors"] = "; ".join(pieces)

        for name in ("citation_publication_date", "citation_date"):
            meta = soup.find("meta", attrs={"name": name})
            if meta and meta.get("content"):
                parsed = self._parse_date(meta["content"])
                if parsed:
                    detail["published_date"] = parsed
                    break

        doi_meta = soup.find("meta", attrs={"name": "citation_doi"})
        if doi_meta and doi_meta.get("content"):
            detail["doi"] = self._one_line(doi_meta["content"])

        for link in soup.find_all("a", href=True):
            candidate = self._absolute_url(link["href"], url)
            if self._looks_like_pdf_or_download(candidate):
                detail["pdf_url"] = candidate
                detail["original_filename"] = self._filename_from_url(candidate)
                break

        detail["metadata"].update({
            "detail_parser": "html",
            "detail_url": url,
        })
        return detail

    def _parse_pdf_detail(self, url, item):
        pdf_path, headers = self._download_pdf(url, referer=_START_URL)
        if not pdf_path:
            raise RuntimeError(f"PDF download failed for {url}")
        try:
            abstract, pdf_meta = self._extract_pdf_text_and_meta(pdf_path)
        finally:
            try:
                os.unlink(pdf_path)
            except Exception:
                pass

        original_filename = (
            self._filename_from_headers(headers)
            or self._filename_from_url(url)
        )
        published_from_pdf = self._parse_pdf_date(
            pdf_meta.get("CreationDate") or pdf_meta.get("ModDate")
        )
        return {
            "title": item.get("title"),
            "abstract": abstract,
            "authors": pdf_meta.get("Author") or None,
            "publisher": _MINISTRY,
            "published_date": item.get("published_date") or published_from_pdf,
            "listed_date": item.get("listed_date"),
            "pdf_url": url,
            "original_filename": original_filename,
            "journal": None,
            "doi": None,
            "metadata": {
                "detail_parser": "pdf",
                "content_disposition_filename": original_filename,
                "pdf_title": pdf_meta.get("Title"),
                "pdf_author": pdf_meta.get("Author"),
                "pdf_creator": pdf_meta.get("Creator"),
                "pdf_producer": pdf_meta.get("Producer"),
                "pdf_pages": pdf_meta.get("Pages"),
                "pdf_creation_date": pdf_meta.get("CreationDate"),
                "pdf_mod_date": pdf_meta.get("ModDate"),
            },
        }

    @staticmethod
    def _extract_pdf_text_and_meta(pdf_path):
        meta = {}
        try:
            result = subprocess.run(
                ["pdfinfo", pdf_path],
                capture_output=True,
                timeout=20,
                check=False,
            )
            raw_info = result.stdout.decode("utf-8", errors="replace")
            for line in raw_info.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
        except Exception:
            pass

        text = ""
        for pages in (2, 5, 0):
            cmd = ["pdftotext"]
            if pages:
                cmd.extend(["-l", str(pages)])
            cmd.extend([pdf_path, "-"])
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=45, check=False)
            except Exception:
                continue
            if result.returncode != 0 or not result.stdout:
                continue
            candidate = result.stdout.decode("utf-8", errors="replace")
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if len(candidate) >= _ABSTRACT_MIN_CHARS:
                text = candidate[:_PDF_ABSTRACT_CHARS]
                if len(text) >= _ABSTRACT_TARGET_CHARS:
                    break
        return text, meta

    def _fallback_abstract(self, item):
        parts = [
            item.get("title"),
            item.get("row_text"),
            f"Kategorie: {item.get('category')}" if item.get("category") else None,
            f"Datum: {item.get('row_date_raw') or item.get('raw_date')}" if (item.get("row_date_raw") or item.get("raw_date")) else None,
            f"Kosten laut Liste: {item.get('cost_raw')}" if item.get("cost_raw") else None,
            "Quelle: Studien des Bundesministeriums für Arbeit, Soziales, Gesundheit, Pflege und Konsumentenschutz.",
        ]
        text = " ".join(self._one_line(p) for p in parts if p)
        return text[:_PDF_ABSTRACT_CHARS]

    def _build_paper(self, item, detail):
        url = item["url"]
        external_id, post_number, native_meta = self._extract_native_ids(url)
        original_filename = detail.get("original_filename") or self._filename_from_url(detail.get("pdf_url") or url)
        listed_date = detail.get("listed_date") or item.get("listed_date")
        published_date = detail.get("published_date") or item.get("published_date") or listed_date
        abstract = self._one_line(detail.get("abstract")) or self._fallback_abstract(item)
        if len(abstract) < _ABSTRACT_TARGET_CHARS:
            fallback = self._fallback_abstract(item)
            if len(fallback) > len(abstract):
                abstract = fallback

        metadata = {
            "posted_date": item.get("row_date_raw") or item.get("raw_date"),
            "originalFilename": original_filename,
            "journal_raw": detail.get("journal"),
            "series": item.get("series"),
            "volume": item.get("volume"),
            "issue": None,
            "node_id": native_meta.get("jcr_id"),
            "jcr_id": native_meta.get("jcr_id"),
            "publicationId": native_meta.get("publicationId"),
            "eprint_id": native_meta.get("eprint_id"),
            "post_number": post_number,
            "list_url": _START_URL,
            "source": item.get("source"),
            "row_index": item.get("row_index"),
            "link_index": item.get("link_index"),
            "row_text": item.get("row_text"),
            "date_raw": item.get("raw_date"),
            "row_date_raw": item.get("row_date_raw"),
            "cost_raw": item.get("cost_raw"),
            "group_label": item.get("group_label"),
            "list_title": item.get("list_title"),
            "url_path": urlparse(url).path,
        }
        detail_meta = detail.get("metadata") or {}
        if isinstance(detail_meta, dict):
            metadata.update(detail_meta)
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        keywords = []
        if item.get("category"):
            keywords.append(item["category"])
        if item.get("series"):
            keywords.append(item["series"])

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, self._url_key(url))),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": detail.get("title") or item.get("title") or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors"),
            "publisher": detail.get("publisher") or _MINISTRY,
            "department": None,
            "journal": detail.get("journal"),
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "keywords": ", ".join(dict.fromkeys(keywords)) if keywords else None,
            "category": item.get("category"),
            "doi": detail.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = 1
        seen_urls = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _MAX_PAGES:
            if time.time() - start_time > _MAX_WALL_SECONDS - 30:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly.")
                break
            if limit is not None and saved >= limit:
                break

            list_url = _START_URL if page == 1 else f"{_START_URL}?page={page}"
            html = self._curl_text(list_url, timeout=60)
            if not html:
                print(f"[{_SITE_ID}] page {page}: failed to fetch list page; stopping.")
                break

            items, has_next = self._parse_list_page(html, list_url)
            if not items:
                print(f"[{_SITE_ID}] page {page}: 0 records; stopping.")
                break

            new_items = []
            for item in items:
                key = self._url_key(item["url"])
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                new_items.append(item)

            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen; stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            for item_index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS - 30:
                    print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly.")
                    return saved

                try:
                    detail = self._parse_detail(item)
                    paper = self._build_paper(item, detail)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] abstract too short ({len(abstract)} chars), "
                            f"skipping: {paper.get('title', '')[:80]}"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_index} failed: {exc}")
                    continue

                time.sleep(self._detail_delay)

            if not has_next:
                break
            page += 1

        if page > _MAX_PAGES:
            print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping.")

        print(f"[{_SITE_ID}] crawl complete: {saved} documents saved.")
        return saved

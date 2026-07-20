# -*- coding: utf-8 -*-
"""Crawler for INFOR WEF publication downloads."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class WefInforClIndexphpCrawler(BaseCrawler):
    site_id = "wef-infor-cl-indexphp"
    site_name = "Custom: wef-infor-cl-indexphp"
    base_url = "https://wef.infor.cl"

    START_URL = "https://wef.infor.cl/index.php/publicaciones"
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    MONTHS = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "setiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }

    def crawl(self, limit=None):
        saved = 0
        item_number = 0

        raw = self._curl_get(self.START_URL, context="publicaciones list")
        if not raw:
            print(f"[{self.site_id}] list endpoint failed; stopping")
            return saved

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] list HTML could not be parsed; stopping")
            return saved

        records = self._parse_list(soup)
        if not records:
            print(f"[{self.site_id}] no publication records found; stopping")
            return saved
        print(f"[{self.site_id}] discovered {len(records)} publication series")

        for record in records:
            if limit is not None and saved >= limit:
                break

            item_number += 1
            try:
                time.sleep(self._delay)

                detail_url = record.get("detail_url") or record.get("url")
                if not detail_url:
                    raise RuntimeError("record has no detail URL")

                detail_raw = self._curl_get(
                    detail_url,
                    context=f"item {item_number} detail",
                    referer=self.START_URL,
                )
                if not detail_raw:
                    raise RuntimeError("detail endpoint failed after retries")

                detail_soup = self._make_soup(detail_raw)
                if detail_soup is None:
                    raise RuntimeError("detail HTML could not be parsed")

                detail = self._parse_detail(detail_soup, detail_url, record)
                issues = detail.get("issues") or []
                if not issues and record.get("pdf_url"):
                    issues = [{
                        "label": record.get("latest_label") or "",
                        "pdf_url": record.get("pdf_url") or "",
                        "cover_image_url": record.get("cover_image_url") or "",
                        "file_query": self._query_dict(record.get("pdf_url") or ""),
                    }]

                if not issues:
                    raise RuntimeError("detail page contained no file records")

                for issue in issues:
                    if limit is not None and saved >= limit:
                        break
                    try:
                        abstract = detail.get("abstract") or record.get("abstract") or ""
                        abstract = self._clean_text(abstract)
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_number} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        issue_label = self._clean_text(issue.get("label") or "")
                        title = self._record_title(detail.get("title") or record["title"], issue_label)
                        pdf_url = issue.get("pdf_url") or ""
                        file_query = issue.get("file_query") or self._query_dict(pdf_url)
                        published_date = self._parse_issue_date(issue_label or file_query.get("n", ""))
                        external_id = self._external_id(detail_url, file_query, issue_label)

                        metadata = {
                            "seriesTitle": detail.get("title") or record.get("title"),
                            "issueLabel": issue_label,
                            "category": detail.get("category") or record.get("category"),
                            "listEndpoint": self.START_URL,
                            "detailEndpoint": detail_url,
                            "fileEndpoint": self._file_endpoint(pdf_url),
                            "fileQuery": file_query,
                            "coverImageUrl": issue.get("cover_image_url") or "",
                            "source": "HTML list/detail pages plus com_wef GetFile endpoint",
                            "abstractLength": len(abstract),
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "title": title,
                            "authors": json.dumps(["Instituto Forestal (INFOR)"], ensure_ascii=False),
                            "abstract": abstract,
                            "category": metadata["category"] or "",
                            "keywords": json.dumps(self._keywords(metadata["category"], detail.get("title")), ensure_ascii=False),
                            "published_date": published_date,
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "doi": "",
                            "department": "Área de Información y Economía Forestal, Instituto Forestal - INFOR",
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {title[:90]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_number} failed: {exc}")
                        continue
            except Exception as exc:
                print(f"[{self.site_id}] item {item_number} failed: {exc}")
                continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _curl_get(self, url, context="request", accept=None, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: es-CL,es;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt}/3): {last_error}"
            )
            if attempt < 3:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    def _parse_list(self, soup):
        records = []
        for card in soup.select("div.card.publicacion"):
            title_link = card.select_one("h5.card-title a[href]")
            if not title_link:
                continue

            title = self._clean_text(title_link.get_text(" ", strip=True))
            detail_url = self._normalize_url(urljoin(self.START_URL, title_link.get("href", "")))
            if not title or not detail_url:
                continue

            download = card.select_one('a[download][href*="task=GetFile"]')
            pdf_url = ""
            latest_label = ""
            if download:
                pdf_url = self._normalize_url(urljoin(self.START_URL, download.get("href", "")))
                latest_label = self._query_dict(pdf_url).get("n", "")

            image = card.select_one("img[src]")
            cover_image_url = ""
            if image and image.get("src"):
                cover_image_url = self._normalize_url(urljoin(self.START_URL, image.get("src")))

            records.append({
                "title": title,
                "detail_url": detail_url,
                "url": detail_url,
                "pdf_url": pdf_url,
                "latest_label": latest_label,
                "abstract": self._list_abstract(card),
                "category": self._category_from_url(detail_url),
                "cover_image_url": cover_image_url,
            })
        return records

    def _parse_detail(self, soup, detail_url, record):
        article = soup.select_one("article.page-content") or soup
        title_node = article.select_one("h1.page-content__title") or article.select_one("h1")
        title = self._clean_text(title_node.get_text(" ", strip=True)) if title_node else record["title"]
        if not title:
            title = record["title"]

        abstract = self._detail_abstract(article) or record.get("abstract") or ""
        category = self._breadcrumb_category(article) or record.get("category") or self._category_from_url(detail_url)

        issues = []
        for download in article.select('a[download][href*="task=GetFile"]'):
            pdf_url = self._normalize_url(urljoin(detail_url, download.get("href", "")))
            issue_card = download.find_parent("div", class_=lambda value: value and "card" in value.split())
            label = ""
            cover_image_url = ""
            if issue_card:
                label_node = issue_card.select_one("h6")
                if label_node:
                    label = self._clean_text(label_node.get_text(" ", strip=True))
                image = issue_card.select_one("img[src]")
                if image and image.get("src"):
                    cover_image_url = self._normalize_url(urljoin(detail_url, image.get("src")))

            if not label:
                label = self._query_dict(pdf_url).get("n", "")

            issues.append({
                "label": label,
                "pdf_url": pdf_url,
                "cover_image_url": cover_image_url,
                "file_query": self._query_dict(pdf_url),
            })

        return {
            "title": title,
            "abstract": abstract,
            "category": category,
            "issues": issues,
        }

    def _list_abstract(self, card):
        body = card.select_one(".card-body")
        if not body:
            return ""

        parts = []
        for p_tag in body.find_all("p", recursive=False):
            classes = p_tag.get("class") or []
            if "text-end" in classes:
                continue
            text = self._clean_text(p_tag.get_text(" ", strip=True))
            if text and text.lower() not in {"descargar", "ver más", "ver mas"}:
                parts.append(text)
        return self._clean_text(" ".join(parts))

    def _detail_abstract(self, article):
        desc_card = None
        for child in article.find_all("div", class_=lambda value: value and "card" in value.split(), recursive=False):
            if child.select_one(".card-body"):
                desc_card = child
                break
        if desc_card is None:
            desc_card = article.select_one(".card .card-body")
        if desc_card is None:
            return ""

        body = desc_card.select_one(".card-body") or desc_card
        return self._clean_text(body.get_text(" ", strip=True))

    def _breadcrumb_category(self, article):
        crumbs = [self._clean_text(node.get_text(" ", strip=True)) for node in article.select(".breadcrumb li")]
        crumbs = [crumb for crumb in crumbs if crumb]
        for idx, crumb in enumerate(crumbs):
            if crumb.lower() == "publicaciones" and idx + 1 < len(crumbs) - 1:
                return crumbs[idx + 1]
        return ""

    def _record_title(self, series_title, issue_label):
        series_title = self._clean_text(series_title)
        issue_label = self._clean_text(issue_label)
        if issue_label and issue_label.lower() not in series_title.lower():
            return f"{series_title} - {issue_label}"
        return series_title

    def _parse_issue_date(self, label):
        text = self._clean_text(label).lower()
        for month_name, month_number in self.MONTHS.items():
            match = re.search(rf"\b{month_name}\s+((?:19|20)\d{{2}})\b", text)
            if match:
                return f"{match.group(1)}-{month_number:02d}-01"

        years = re.findall(r"(?:19|20)\d{2}", text)
        if years:
            return f"{years[-1]}-01-01"
        return ""

    def _external_id(self, detail_url, file_query, issue_label):
        slug = self._slug_from_url(detail_url)
        query_parts = [
            file_query.get("id", ""),
            file_query.get("f", ""),
            file_query.get("n", "") or issue_label,
        ]
        raw = ":".join([slug] + [part for part in query_parts if part])
        safe = re.sub(r"[^0-9A-Za-z._:-]+", "-", raw).strip("-").lower()
        safe = re.sub(r"-{2,}", "-", safe)
        return safe[:180] or slug

    def _slug_from_url(self, url):
        path = urlsplit(url).path.rstrip("/")
        slug = path.split("/")[-1] if path else "publication"
        return re.sub(r"[^0-9A-Za-z._:-]+", "-", slug).strip("-").lower() or "publication"

    def _category_from_url(self, url):
        parts = [part for part in urlsplit(url).path.split("/") if part]
        try:
            idx = parts.index("publicaciones")
        except ValueError:
            return ""
        if idx + 1 >= len(parts):
            return ""
        return self._clean_text(parts[idx + 1].replace("-", " ").title())

    def _keywords(self, category, title):
        values = [
            "Estadísticas forestales",
            "Sector forestal chileno",
            category or "",
            title or "",
        ]
        seen = set()
        keywords = []
        for value in values:
            clean = self._clean_text(value)
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                keywords.append(clean)
        return keywords

    def _file_endpoint(self, pdf_url):
        if not pdf_url:
            return ""
        parts = urlsplit(pdf_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def _query_dict(self, url):
        if not url:
            return {}
        return {key: value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)}

    def _normalize_url(self, url):
        if not url:
            return ""
        parts = urlsplit(url)
        query = urlencode(parse_qsl(parts.query, keep_blank_values=True), doseq=True)
        path = quote(parts.path, safe="/%")
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

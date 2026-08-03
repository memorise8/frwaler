# -*- coding: utf-8 -*-
"""Jeju Special Self-Governing Province press release crawler.

Target: https://www.jeju.go.kr/news/bodo/list.htm

List endpoint   : GET /news/bodo/list.htm?page={n}
Detail endpoint : GET /news/bodo/list.htm?act=view&seq={native_id}
Download        : GET /news/bodo/list.htm?act=download&seq={native_id}&no={n}
"""

from __future__ import annotations

import email.utils
import html as html_module
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://www.jeju.go.kr"
_LIST_PATH = "/news/bodo/list.htm"
_START_URL = f"{_BASE_URL}{_LIST_PATH}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _clean_text(value):
    if value is None:
        return ""
    text = html_module.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(value):
    text = _clean_text(value)
    if not text:
        return None

    match = re.search(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed:
            return parsed.date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    return None


def _html_fragment_to_text(raw):
    text = html_module.unescape(raw or "")
    try:
        from bs4 import BeautifulSoup

        return _clean_text(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
        return _clean_text(text)


def _download_filename(text):
    name = _clean_text(text)
    name = re.sub(r"^바로보기\s*", "", name)
    name = re.sub(r"\s*\([\d.,]+\s*[KMGT]?\s*Bytes?\)\s*$", "", name, flags=re.I)
    name = re.sub(r"\s*\[[^\]]*Bytes?\]\s*$", "", name, flags=re.I)
    return name.strip() or None


class JejuGoKrNewsCrawler(BaseCrawler):
    site_id = "jeju-go-kr-news"
    site_name = "Custom: jeju-go-kr-news"
    base_url = _BASE_URL

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _TIME_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _TIME_EXIT_MARGIN_SECONDS = 30

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(1, self._MAX_PAGES + 1):
            if self._near_time_budget(started_at):
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                break
            if limit is not None and saved >= limit:
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_text(list_url, context=f"list page {page}", timeout=45)
            if not raw:
                print(f"[{self.site_id}] list page {page} returned no body; stopping")
                break

            soup = self._soup(raw, context=f"list page {page}")
            items, has_next = self._parse_list_page(soup, page, list_url)
            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                break

            new_candidates = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._near_time_budget(started_at):
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                detail_url = item.get("url")
                item_label = item.get("seq") or item.get("title") or detail_url or "unknown"
                if not detail_url:
                    print(f"[{self.site_id}] item {item_label} missing detail URL; skipping")
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_candidates += 1

                try:
                    paper = self._process_item(item)
                    if not paper:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            if new_candidates == 0:
                print(f"[{self.site_id}] page {page} had no unseen records; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page} has no next-page link; stopping")
                break
            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages")

        return saved

    def _process_item(self, item):
        detail_url = item["url"]
        seq = item.get("seq")
        raw = self._curl_text(
            detail_url,
            context=f"detail {seq or detail_url}",
            referer=item.get("list_url") or _START_URL,
            timeout=45,
        )
        if not raw:
            print(f"[{self.site_id}] item {seq or detail_url} failed: detail fetch returned no body")
            return None

        soup = self._soup(raw, context=f"detail {seq or detail_url}")
        detail = self._parse_detail_page(soup, item)

        abstract = detail.get("abstract") or item.get("abstract") or ""
        title = detail.get("title") or item.get("title") or "(untitled)"
        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        department = detail.get("department") or item.get("department")
        attachments = detail.get("attachments") or []
        pdf_attachment = self._select_pdf_attachment(attachments)
        pdf_url = pdf_attachment["url"] if pdf_attachment else None
        original_filename = pdf_attachment["filename"] if pdf_attachment else None

        metadata = {
            "seq": seq,
            "post_number": seq,
            "detail_url": detail_url,
            "list_url": item.get("list_url"),
            "page": item.get("page"),
            "title_from_list": item.get("title"),
            "subtitle_from_list": item.get("subtitle"),
            "list_abstract": item.get("abstract"),
            "department_from_list": item.get("department"),
            "posted_date": item.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "published_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "attachments": attachments,
            "contact": detail.get("contact"),
            "views": detail.get("views"),
            "category": "보도자료",
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        return {
            "id": f"{self.site_id}:{seq}" if seq else None,
            "site_id": self.site_id,
            "external_id": seq,
            "post_number": seq,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": department,
            "publisher": "제주특별자치도",
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": "보도자료",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _curl_text(self, url, *, context, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--http1.1",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {_UA}",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = None
        waits = (1, 3, 9)
        for attempt in range(3):
            if attempt:
                time.sleep(waits[attempt - 1])
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                stderr = result.stderr.decode("utf-8", errors="replace")
                last_error = stderr.strip() or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] curl {context} attempt {attempt + 1}/3 failed: "
                f"{last_error}"
            )

        print(f"[{self.site_id}] curl {context} failed after 3 attempts: {url}")
        return None

    def _soup(self, raw, *, context):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup import failed for {context}: {exc}")
            return None

        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")

        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_error}")
        try:
            return BeautifulSoup("", "html.parser")
        except Exception:
            return None

    def _parse_list_page(self, soup, page, list_url):
        if soup is None:
            return [], False

        rows = []
        for li in soup.select("ul.newsBoardBox li.board-news__article"):
            link = li.find("a", href=True)
            if not link:
                continue
            href = link.get("href") or ""
            seq = self._seq_from_url(href)
            if not seq:
                continue

            title_node = li.select_one("strong.text-ellipsis") or link
            title = _clean_text(title_node.get_text(" ", strip=True))
            subtitles = [
                _clean_text(node.get_text(" ", strip=True))
                for node in li.select("span.tit2")
                if _clean_text(node.get_text(" ", strip=True))
            ]
            list_abstract_node = li.select_one("span.txt")
            list_abstract = (
                _html_fragment_to_text(list_abstract_node.decode_contents())
                if list_abstract_node
                else ""
            )
            department, listed_raw = self._parse_list_date(li.select_one("span.date"))
            listed_date = _parse_date(listed_raw)

            rows.append(
                {
                    "seq": seq,
                    "page": page,
                    "list_url": list_url,
                    "url": self._detail_url(seq),
                    "title": title,
                    "subtitle": " ".join(subtitles).strip(),
                    "abstract": list_abstract,
                    "department": department,
                    "listed_date_raw": listed_raw,
                    "listed_date": listed_date,
                }
            )

        return rows, self._has_next_page(soup, page)

    def _parse_detail_page(self, soup, item):
        if soup is None:
            return {}

        title = ""
        head = soup.select_one(".news-head")
        if head:
            title_node = head.find("h2")
            if title_node:
                title = _clean_text(title_node.get_text(" ", strip=True))
                title = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()
        if not title:
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = _clean_text((og_title.get("content") or "").split("|")[0])

        subtitle = ""
        subtitle_node = soup.select_one(".news-head .sub-title")
        if subtitle_node:
            subtitle = _clean_text(subtitle_node.get_text(" ", strip=True))

        abstract = self._article_text(soup)
        if subtitle and subtitle not in abstract:
            abstract = _clean_text(f"{subtitle} {abstract}")
        if len(abstract) < 50:
            og_description = soup.find("meta", property="og:description")
            if og_description:
                abstract = _html_fragment_to_text(og_description.get("content") or "")

        info = self._parse_detail_info(soup)
        attachments = self._parse_attachments(soup, item.get("seq"))

        return {
            "title": title,
            "abstract": abstract,
            "published_date": _parse_date(info.get("published_date_raw")),
            "published_date_raw": info.get("published_date_raw"),
            "department": info.get("department") or item.get("department"),
            "contact": info.get("contact"),
            "views": info.get("views"),
            "attachments": attachments,
        }

    def _article_text(self, soup):
        article = soup.select_one("#articleContents")
        if not article:
            return ""

        try:
            from bs4 import BeautifulSoup

            article = BeautifulSoup(str(article), "html.parser")
        except Exception:
            pass

        for selector in ("script", "style", "iframe", ".file-preview", ".thumbnail", "#hwpEditorBoardContent"):
            for node in article.select(selector):
                node.decompose()
        return _clean_text(article.get_text(" ", strip=True))

    def _parse_detail_info(self, soup):
        info = {}
        for p in soup.select(".news-info .info-line p"):
            label_node = p.select_one(".info-name")
            if not label_node:
                continue
            label = _clean_text(label_node.get_text(" ", strip=True))
            text = _clean_text(p.get_text(" ", strip=True))
            value = _clean_text(text.replace(label, "", 1).lstrip("|"))
            if label == "문의처":
                info["contact"] = value
                if "/" in value:
                    dept = _clean_text(value.rsplit("/", 1)[-1])
                    if dept:
                        info["department"] = dept
            elif label == "조회":
                info["views"] = value
            elif label == "작성일":
                info["published_date_raw"] = value
        return info

    def _parse_attachments(self, soup, seq):
        attachments = []
        if not seq:
            return attachments

        for link in soup.find_all("a", href=True):
            href = link.get("href") or ""
            if "act=download" not in href or f"seq={seq}" not in href:
                continue
            filename = _download_filename(link.get_text(" ", strip=True))
            url = urljoin(_BASE_URL, href)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            attachments.append(
                {
                    "filename": filename,
                    "url": url,
                    "seq": query.get("seq", [seq])[0],
                    "no": query.get("no", [None])[0],
                }
            )
        return attachments

    def _select_pdf_attachment(self, attachments):
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            if filename.endswith(".pdf"):
                return attachment
        return None

    def _parse_list_date(self, date_node):
        if not date_node:
            return None, None
        text = _clean_text(date_node.get_text(" ", strip=True))
        if "|" in text:
            department, raw_date = text.rsplit("|", 1)
            return _clean_text(department), _clean_text(raw_date)
        return None, text or None

    def _has_next_page(self, soup, page):
        for link in soup.find_all("a", href=True):
            text = _clean_text(link.get_text(" ", strip=True))
            title = _clean_text(link.get("title"))
            href = link.get("href") or ""
            if "다음" in text or "다음" in title:
                return True
            seq = self._page_from_url(href)
            if seq and seq > page:
                return True
        return False

    def _near_time_budget(self, started_at):
        elapsed = time.time() - started_at
        return elapsed >= self._TIME_BUDGET_SECONDS - self._TIME_EXIT_MARGIN_SECONDS

    def _list_url(self, page):
        if page <= 1:
            return _START_URL
        return f"{_START_URL}?page={page}"

    def _detail_url(self, seq):
        return f"{_START_URL}?act=view&seq={seq}"

    def _seq_from_url(self, url):
        parsed = urlparse(urljoin(_BASE_URL, url))
        values = parse_qs(parsed.query).get("seq")
        if not values:
            return None
        seq = values[0].strip()
        return seq if seq else None

    def _page_from_url(self, url):
        parsed = urlparse(urljoin(_BASE_URL, url))
        values = parse_qs(parsed.query).get("page")
        if not values:
            return None
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return None

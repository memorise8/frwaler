# -*- coding: utf-8 -*-
"""Crawler for OpenAIRE's Open Science Slovenia country page."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


class OpenaireEuOsSloveniaCrawler(BaseCrawler):
    site_id = "openaire-eu-os-slovenia"
    site_name = "Custom: openaire-eu-os-slovenia"
    base_url = "https://www.openaire.eu"

    START_URL = "https://www.openaire.eu/os-slovenia"
    FALLBACK_IFRAME_URL = "https://oseurope.openaire.eu/embeddable/country/SI"
    OSEUROPE_BASE = "https://oseurope.openaire.eu"

    SURVEYS_API_URL = OSEUROPE_BASE + "/api/surveys"
    ANSWER_API_URL = OSEUROPE_BASE + "/api/answers/public"
    ANSWER_METADATA_API_URL = OSEUROPE_BASE + "/api/answers/public/metadata"
    MANAGERS_API_URL = OSEUROPE_BASE + "/api/stakeholders/sh-country-SI/managers/public"
    OSO_STATS_API_URL = "https://services.openaire.eu/stats-tool/raw"

    COUNTRY_CODE = "SI"
    COUNTRY_NAME = "Slovenia"
    STAKEHOLDER_ID = "sh-country-SI"

    PAGE_SIZE = 10
    MAX_PAGES = 200
    MAX_SECONDS = 25 * 60
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        item_no = 0
        limit_or_inf = str(limit) if limit is not None else "inf"

        context = self._fetch_context()
        records = self._build_records(context)
        if not records:
            print(f"[{self.site_id}] no records discovered")
            return 0

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > self.MAX_SECONDS - 10:
                print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            start = (page - 1) * self.PAGE_SIZE
            end = start + self.PAGE_SIZE
            page_records = records[start:end]
            if not page_records:
                break

            new_on_page = 0
            for record in page_records:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self.MAX_SECONDS - 10:
                    print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                    return saved

                item_no += 1
                url = record.get("url")
                if url in seen_urls:
                    print(f"[{self.site_id}] item {item_no} skipped: duplicate URL {url}")
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    paper = self._record_to_paper(record)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_no} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_no} skipped: "
                            f"abstract below save threshold ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_no} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network and endpoint discovery
    # ------------------------------------------------------------------

    def _fetch_context(self):
        start_html = self._curl_get(self.START_URL, context="start page", accept="text/html,*/*;q=0.8") or ""
        iframe_url = self._discover_iframe_url(start_html) or self.FALLBACK_IFRAME_URL
        page_meta = self._extract_page_metadata(start_html)

        iframe_html = self._curl_get(iframe_url, context="embedded country app", accept="text/html,*/*;q=0.8") or ""
        survey_data = self._fetch_surveys()
        survey = self._select_country_survey(survey_data)
        if not survey:
            raise RuntimeError("country survey not found in /api/surveys response")

        survey_id = survey.get("id")
        answer = self._fetch_answer(survey_id) or {}
        answer_metadata = self._fetch_answer_metadata(survey_id) or {}
        managers = self._fetch_managers() or []
        stats = self._fetch_country_stats() or {}

        return {
            "start_html": start_html,
            "iframe_html": iframe_html,
            "iframe_url": iframe_url,
            "page_meta": page_meta,
            "survey_data": survey_data,
            "survey": survey,
            "survey_id": survey_id,
            "answer": answer,
            "answer_metadata": answer_metadata,
            "managers": managers,
            "stats": stats,
        }

    def _fetch_surveys(self):
        params = {"type": "country", "order": "desc", "sort": "creationDate"}
        return self._curl_json(f"{self.SURVEYS_API_URL}?{urlencode(params)}", context="survey list")

    def _fetch_answer(self, survey_id):
        params = {"stakeholderId": self.STAKEHOLDER_ID, "surveyId": survey_id}
        return self._curl_json(f"{self.ANSWER_API_URL}?{urlencode(params)}", context="public answer detail")

    def _fetch_answer_metadata(self, survey_id):
        params = {"stakeholderId": self.STAKEHOLDER_ID, "surveyId": survey_id}
        return self._curl_json(
            f"{self.ANSWER_METADATA_API_URL}?{urlencode(params)}",
            context="public answer metadata",
        )

    def _fetch_managers(self):
        return self._curl_json(self.MANAGERS_API_URL, context="stakeholder managers")

    def _fetch_country_stats(self):
        query = {
            "series": [
                {"query": {"name": "oso.rnd.country", "parameters": [self.COUNTRY_CODE], "profile": "observatory"}},
                {"query": {"name": "oso.funder.country", "parameters": [self.COUNTRY_CODE], "profile": "observatory"}},
                {"query": {"name": "oso.funding_organizations.country", "parameters": [self.COUNTRY_CODE], "profile": "observatory"}},
                {"query": {"name": "oso.ec_funded_organizations.country", "parameters": [self.COUNTRY_CODE], "profile": "observatory"}},
                {"query": {"name": "new.oso.ec_funded_projects.country", "parameters": [self.COUNTRY_CODE], "profile": "observatory"}},
            ],
            "verbose": True,
        }
        url = f"{self.OSO_STATS_API_URL}?json={quote(json.dumps(query, separators=(',', ':')))}"
        return self._curl_json(url, context="country stats")

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(url, context=context, accept="application/json,text/javascript,*/*;q=0.8")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None

    def _curl_get(self, url, context="request", accept="application/json,text/html,*/*;q=0.8"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--fail",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-H",
            f"Referer: {self.START_URL}",
            url,
        ]

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed attempt "
                    f"{attempt}/{len(self.BACKOFF_SECONDS)}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _build_records(self, context):
        survey = context["survey"]
        answer = context["answer"]
        answer_metadata = context["answer_metadata"]
        managers = context["managers"]
        field_infos = self._ordered_field_infos(survey)
        editor_names = self._editor_names(answer_metadata, managers)
        listed_date_raw = answer_metadata.get("lastUpdate") or survey.get("modificationDate")
        listed_date = self._parse_iso_date(listed_date_raw)
        page_meta = context["page_meta"]

        records = []
        for info in field_infos:
            top_section = info.get("top_section")
            field_name = info.get("name")
            if not top_section or not field_name:
                continue
            section_answers = answer.get(top_section)
            if not isinstance(section_answers, dict) or field_name not in section_answers:
                continue

            raw_value = section_answers.get(field_name)
            text_parts = self._collect_text_parts(raw_value)
            abstract = self._normalize_text("\n\n".join(text_parts))
            if not abstract:
                continue

            links = self._collect_links(raw_value)
            pdf_url = next((u for u in links if self._looks_like_pdf(u)), None)
            original_filename = self._filename_from_url(pdf_url)
            field_id = str(info.get("id") or "").strip()
            post_number = field_id or self._extract_number(field_name) or field_name
            label = self._clean_label(info.get("label") or field_name)
            title = f"{self.COUNTRY_NAME}: {label}"
            item_url = f"{self.START_URL}#question-{post_number}"
            native_id = f"{self.STAKEHOLDER_ID}:{context['survey_id']}:{field_name}"

            metadata = {
                "posted_date": listed_date_raw,
                "originalFilename": original_filename,
                "journal_raw": None,
                "series": survey.get("series"),
                "volume": None,
                "issue": None,
                "native_id": native_id,
                "post_number": post_number,
                "country_code": self.COUNTRY_CODE,
                "country_name": self.COUNTRY_NAME,
                "stakeholder_id": self.STAKEHOLDER_ID,
                "survey_id": context["survey_id"],
                "survey_name": survey.get("name"),
                "survey_creation_date": survey.get("creationDate"),
                "survey_modification_date": survey.get("modificationDate"),
                "question_name": field_name,
                "question_id": field_id,
                "question_label": info.get("label"),
                "section": top_section,
                "section_path": info.get("section_path"),
                "subsection": info.get("subsection"),
                "source_start_url": self.START_URL,
                "iframe_url": context["iframe_url"],
                "list_api_url": self.SURVEYS_API_URL,
                "detail_api_url": self.ANSWER_API_URL,
                "metadata_api_url": self.ANSWER_METADATA_API_URL,
                "managers_api_url": self.MANAGERS_API_URL,
                "stats_api_url": self.OSO_STATS_API_URL,
                "external_links": [u for u in links if u != pdf_url],
                "joomla_article_id": page_meta.get("joomla_article_id"),
                "joomla_date_created": page_meta.get("dateCreated"),
                "joomla_date_modified": page_meta.get("dateModified"),
                "answer_metadata": answer_metadata,
                "managers": managers,
                "country_stats": context.get("stats"),
                "survey_field": info.get("raw_field"),
                "raw_value": raw_value,
            }

            records.append(
                {
                    "id": self._stable_id(native_id),
                    "site_id": self.site_id,
                    "external_id": post_number,
                    "native_id": native_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": listed_date,
                    "listed_date": listed_date,
                    "posted_date": listed_date,
                    "authors": "; ".join(editor_names) if editor_names else None,
                    "publisher": "OpenAIRE",
                    "department": info.get("subsection") or top_section,
                    "journal": None,
                    "url": item_url,
                    "pdf_url": pdf_url,
                    "keywords": ", ".join(
                        [
                            "Open Science",
                            "Slovenia",
                            "OpenAIRE",
                            "EOSC",
                            self._clean_label(top_section).title(),
                        ]
                    ),
                    "category": top_section,
                    "doi": None,
                    "original_filename": original_filename,
                    "metadata": metadata,
                }
            )
        return records

    def _record_to_paper(self, record):
        metadata = record.get("metadata") or {}
        return {
            "id": record.get("id"),
            "site_id": self.site_id,
            "external_id": record.get("external_id"),
            "post_number": record.get("post_number"),
            "title": record.get("title"),
            "abstract": record.get("abstract"),
            "published_date": record.get("published_date"),
            "listed_date": record.get("listed_date"),
            "posted_date": record.get("posted_date") or record.get("listed_date"),
            "authors": record.get("authors"),
            "publisher": record.get("publisher"),
            "department": record.get("department"),
            "journal": record.get("journal"),
            "url": record.get("url"),
            "pdf_url": record.get("pdf_url"),
            "keywords": record.get("keywords"),
            "category": record.get("category"),
            "doi": record.get("doi"),
            "original_filename": record.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _ordered_field_infos(self, survey):
        infos = []

        def walk_sections(sections, path=()):
            for section in sections or []:
                name = section.get("name") or section.get("id") or ""
                next_path = path + (name,)
                fields = section.get("fields") or []
                for field in sorted(fields, key=lambda f: self._safe_int((f.get("form") or {}).get("display", {}).get("order"))):
                    label = ((field.get("label") or {}).get("text") or field.get("name") or field.get("id") or "").strip()
                    top_section = next_path[0] if next_path else name
                    infos.append(
                        {
                            "id": field.get("id"),
                            "name": field.get("name"),
                            "label": label,
                            "top_section": top_section,
                            "subsection": next_path[-1] if len(next_path) > 1 else None,
                            "section_path": list(next_path),
                            "raw_field": field,
                        }
                    )
                walk_sections(section.get("subSections"), next_path)

        walk_sections(survey.get("sections"))
        return infos

    @staticmethod
    def _select_country_survey(data):
        if not isinstance(data, dict):
            return None
        results = data.get("results") or []
        if not isinstance(results, list):
            return None
        for item in results:
            if isinstance(item, dict) and item.get("type") == "country":
                return item
        return results[0] if results else None

    def _discover_iframe_url(self, html):
        soup = self._make_soup(html)
        if soup:
            for iframe in soup.find_all("iframe"):
                src = iframe.get("src")
                if src and "oseurope.openaire.eu" in src and "/country/SI" in src:
                    return urljoin(self.START_URL, src)
        m = re.search(r'<iframe[^>]+src=["\']([^"\']*oseurope\.openaire\.eu[^"\']*/country/SI[^"\']*)', html or "", re.I)
        return unescape(m.group(1)) if m else None

    def _extract_page_metadata(self, html):
        metadata = {}
        soup = self._make_soup(html)
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"}) if soup else []
        for script in scripts:
            raw = script.string or script.get_text() or ""
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            nodes = []
            if isinstance(data, dict):
                if isinstance(data.get("@graph"), list):
                    nodes.extend(data["@graph"])
                else:
                    nodes.append(data)
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("@type") == "Article" or "/schema/com_content/article/" in str(node.get("@id", "")):
                    article_id = None
                    m = re.search(r"/article/(\d+)", str(node.get("@id", "")))
                    if m:
                        article_id = m.group(1)
                    metadata.update(
                        {
                            "joomla_article_id": article_id,
                            "dateCreated": node.get("dateCreated"),
                            "dateModified": node.get("dateModified"),
                            "headline": node.get("headline"),
                        }
                    )
        return metadata

    def _collect_text_parts(self, value):
        parts = []

        def walk(obj):
            if obj is None:
                return
            if isinstance(obj, str):
                text = self._strip_html(obj)
                if not text:
                    return
                if self._is_url_only(text):
                    return
                if text not in parts:
                    parts.append(text)
                return
            if isinstance(obj, list):
                for item in obj:
                    walk(item)
                return
            if isinstance(obj, dict):
                for key in sorted(obj.keys()):
                    walk(obj[key])

        walk(value)
        return parts

    def _collect_links(self, value):
        links = []

        def add(url):
            if not url:
                return
            url = unescape(str(url)).strip()
            url = url.rstrip(").,;]")
            if not url:
                return
            absolute = urljoin(self.START_URL, url)
            if absolute not in links:
                links.append(absolute)

        def walk(obj):
            if obj is None:
                return
            if isinstance(obj, str):
                soup = self._make_soup(obj)
                if soup:
                    for a in soup.find_all("a", href=True):
                        add(a.get("href"))
                else:
                    for href in re.findall(r'href=["\']([^"\']+)["\']', obj, flags=re.I):
                        add(href)
                for url in re.findall(r"https?://[^\s<>'\"]+", obj):
                    add(url)
                return
            if isinstance(obj, list):
                for item in obj:
                    walk(item)
                return
            if isinstance(obj, dict):
                for item in obj.values():
                    walk(item)

        walk(value)
        return links

    def _strip_html(self, raw):
        if raw is None:
            return ""
        text = str(raw)
        soup = self._make_soup(text)
        if soup:
            for tag in soup.find_all(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
        else:
            text = re.sub(r"<[^>]+>", " ", text)
        return self._normalize_text(text)

    def _make_soup(self, raw):
        if not raw:
            return None
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _normalize_text(text):
        text = unescape(text or "").replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _clean_label(self, label):
        return self._normalize_text(re.sub(r"<[^>]+>", " ", label or ""))

    @staticmethod
    def _parse_iso_date(raw):
        if not raw:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    @staticmethod
    def _extract_number(text):
        m = re.search(r"(\d+)", str(text or ""))
        return m.group(1) if m else None

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 999999

    @staticmethod
    def _stable_id(native_id):
        return hashlib.sha1(str(native_id).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_url_only(text):
        stripped = (text or "").strip()
        return bool(re.fullmatch(r"https?://\S+", stripped))

    @staticmethod
    def _looks_like_pdf(url):
        if not url:
            return False
        parsed = urlparse(url)
        path = parsed.path.lower()
        return path.endswith(".pdf") or ".pdf/" in path

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        filename = unquote(path.rstrip("/").split("/")[-1])
        if filename and "." in filename and len(filename) <= 200:
            return filename
        return None

    @staticmethod
    def _editor_names(answer_metadata, managers):
        names = []
        for source in (answer_metadata.get("editors") if isinstance(answer_metadata, dict) else None, managers):
            if not isinstance(source, list):
                continue
            for person in source:
                if not isinstance(person, dict):
                    continue
                name = person.get("fullname")
                if not name:
                    name = " ".join(
                        part for part in [person.get("name"), person.get("surname")]
                        if part
                    ).strip()
                if name and name not in names:
                    names.append(name)
        return names

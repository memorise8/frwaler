# -*- coding: utf-8 -*-
"""Crawler for data.gov.be Dutch PDF datasets.

The filtered dataset URL is protected by Drupal/TSPD for direct curl, but the
same PDF-filtered collection is exposed as the public taxonomy page
``/nl/file-type/pdf``. Dataset details and distribution URLs are exposed through
Belgium's DCAT Linked Data Fragments endpoint.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_BASE_URL = "https://data.gov.be"
_START_URL = "https://data.gov.be/nl/datasets?f%5B0%5D=format%3A65"
_LIST_URL = "https://data.gov.be/nl/file-type/pdf"
_RSS_URL = "https://data.gov.be/nl/taxonomy/term/65/feed"
_LDF_URL = "https://ldf.belgif.be/datagovbe"
_FORMAT_URI = "http://publications.europa.eu/resource/authority/file-type/PDF"

_MAX_PAGES = 200
_PAGE_SIZE_HINT = 10
_WALL_BUDGET_SECONDS = 25 * 60
_ABSTRACT_MIN_SAVE = 100

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SKOS_PREF_LABEL = "http://www.w3.org/2004/02/skos/core#prefLabel"
FOAF_NAME = "http://xmlns.com/foaf/0.1/name"
FOAF_GIVEN_NAME = "http://xmlns.com/foaf/0.1/givenName"
FOAF_FAMILY_NAME = "http://xmlns.com/foaf/0.1/familyName"
VCARD_FN = "http://www.w3.org/2006/vcard/ns#fn"
VCARD_ORG_NAME = "http://www.w3.org/2006/vcard/ns#organization-name"

DCT_IDENTIFIER = "http://purl.org/dc/terms/identifier"
DCT_TITLE = "http://purl.org/dc/terms/title"
DCT_DESCRIPTION = "http://purl.org/dc/terms/description"
DCT_ISSUED = "http://purl.org/dc/terms/issued"
DCT_CREATED = "http://purl.org/dc/terms/created"
DCT_MODIFIED = "http://purl.org/dc/terms/modified"
DCT_PUBLISHER = "http://purl.org/dc/terms/publisher"
DCT_CREATOR = "http://purl.org/dc/terms/creator"
DCT_TYPE = "http://purl.org/dc/terms/type"
DCT_FORMAT = "http://purl.org/dc/terms/format"
DCT_BIBLIO = "http://purl.org/dc/terms/bibliographicCitation"

DCAT_KEYWORD = "http://www.w3.org/ns/dcat#keyword"
DCAT_DISTRIBUTION = "http://www.w3.org/ns/dcat#distribution"
DCAT_DOWNLOAD_URL = "http://www.w3.org/ns/dcat#downloadURL"
DCAT_ACCESS_URL = "http://www.w3.org/ns/dcat#accessURL"
DCAT_MEDIA_TYPE = "http://www.w3.org/ns/dcat#mediaType"
DCAT_LANDING_PAGE = "http://www.w3.org/ns/dcat#landingPage"
DCAT_CONTACT_POINT = "http://www.w3.org/ns/dcat#contactPoint"
DCAT_THEME = "http://www.w3.org/ns/dcat#theme"

ADMS_IDENTIFIER = "http://www.w3.org/ns/adms#identifier"
SCHEMA_START_DATE = "http://schema.org/startDate"
SCHEMA_END_DATE = "http://schema.org/endDate"
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"


class DataGovBeNlCrawler(BaseCrawler):
    site_id = "data-gov-be-nl"
    site_name = "Custom: data-gov-be-nl"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl the Dutch PDF dataset listing and persist document records."""
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 0
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"
        self._label_cache = {}
        self._subject_cache = {}
        self._triples_cache = {}

        rss_by_slug = self._fetch_rss_map()

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            list_url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
            raw = self._curl_get(list_url, accept="text/html,application/xhtml+xml,*/*;q=0.8")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed or empty; stopping")
                break

            items, has_next = self._parse_list_page(raw, list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_urls_on_page += 1

                slug = item.get("slug") or self._slug_from_url(url)
                if slug in rss_by_slug:
                    item["rss"] = rss_by_slug[slug]

                try:
                    did_save = self._process_item(item)
                    if did_save:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_id = slug or url or "?"
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent; done")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, accept="*/*", timeout=35):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: nl-BE,nl;q=0.9,en;q=0.8",
            url,
        ]
        waits = (1, 3, 9)
        last_error = None
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(waits):
                print(f"[{self.site_id}] network error for {url}: {last_error}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_ldf(self, params):
        query = urlencode(params, quote_via=quote)
        url = f"{_LDF_URL}?{query}"
        # The LDF server's negotiation is strict; adding a broad wildcard can
        # make it return the human HTML view instead of parseable N-Triples.
        raw = self._curl_get(url, accept="application/n-triples")
        if not raw:
            return []
        return self._parse_ntriples(raw)

    # ------------------------------------------------------------------
    # List and RSS parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw, page_url):
        soup = self._make_soup(raw)
        if soup is None:
            return [], False

        rows = soup.select("article.node--type-dataset")
        if not rows:
            rows = soup.select(".view__row")

        items = []
        for row in rows:
            link = row.select_one(".field--name-node-title a[href*='/datasets/']")
            if link is None:
                link = row.select_one("a[href*='/datasets/']")
            if link is None:
                continue

            href = link.get("href")
            url = urljoin(_BASE_URL, href)
            title = self._clean_text(link.get_text(" ", strip=True))
            body = row.select_one(".field--name-body")
            abstract = self._clean_text(body.get_text(" ", strip=True) if body else "")
            slug = self._slug_from_url(url)
            node_id = row.get("data-history-node-id") or row.get("data-node-id")

            if not title or not slug:
                continue

            items.append(
                {
                    "title": title,
                    "abstract": abstract,
                    "url": url,
                    "slug": slug,
                    "node_id": node_id,
                    "source_list_url": page_url,
                }
            )

        has_next = bool(soup.select_one("a[rel='next']"))
        if not has_next:
            for a in soup.select("a[href*='page=']"):
                text = self._clean_text(a.get_text(" ", strip=True)).lower()
                title = (a.get("title") or "").lower()
                if "volgende" in text or "next" in text or "volgende" in title or "next" in title:
                    has_next = True
                    break
        return items, has_next

    def _fetch_rss_map(self):
        raw = self._curl_get(_RSS_URL, accept="application/rss+xml,application/xml,text/xml,*/*;q=0.8")
        if not raw:
            return {}
        soup = self._make_soup(raw, prefer_xml=True)
        if soup is None:
            return {}

        out = {}
        for item in soup.find_all("item"):
            link_tag = item.find("link")
            if link_tag is None:
                continue
            link = self._clean_text(link_tag.get_text(" ", strip=True))
            slug = self._slug_from_url(link)
            if not slug:
                continue

            pub_raw = self._tag_text(item, "pubDate")
            guid = self._tag_text(item, "guid")
            creator = self._tag_text(item, "dc:creator") or self._tag_text(item, "creator")
            node_id = None
            guid_match = re.search(r"\b(\d+)\b", guid or "")
            if guid_match:
                node_id = guid_match.group(1)
            out[slug] = {
                "link": link,
                "pubDate": pub_raw,
                "listed_date": self._parse_date(pub_raw),
                "creator": creator,
                "guid": guid,
                "node_id": node_id,
            }
        return out

    # ------------------------------------------------------------------
    # Per-item detail flow
    # ------------------------------------------------------------------

    def _process_item(self, item):
        time.sleep(getattr(self, "_delay", 1.0))

        slug = item.get("slug") or self._slug_from_url(item.get("url"))
        record = self._build_record(item, slug)
        if not record:
            print(f"[{self.site_id}] item {slug or '?'} skipped: no parseable record")
            return False

        abstract = self._clean_text(record.get("abstract") or "")
        if len(abstract) < _ABSTRACT_MIN_SAVE:
            print(f"[{self.site_id}] item {slug or '?'} skipped: abstract too short ({len(abstract)} chars)")
            return False

        metadata = dict(record.get("metadata") or {})
        metadata.setdefault("posted_date", record.get("posted_date_raw"))
        metadata.setdefault("originalFilename", record.get("original_filename"))
        metadata.setdefault("journal_raw", record.get("journal_raw"))
        metadata.setdefault("series", record.get("series"))
        metadata.setdefault("volume", record.get("volume"))
        metadata.setdefault("issue", record.get("issue"))
        metadata.setdefault("slug", slug)
        metadata.setdefault("node_id", record.get("node_id"))
        metadata.setdefault("post_number", record.get("post_number"))
        metadata.setdefault("source_start_url", _START_URL)
        metadata.setdefault("list_endpoint", _LIST_URL)
        metadata.setdefault("detail_endpoint", _LDF_URL)
        metadata.setdefault("format_term_id", "65")
        metadata.setdefault("format_uri", _FORMAT_URI)
        metadata.setdefault("listed_date", record.get("listed_date"))

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": record.get("external_id") or slug,
            "post_number": record.get("post_number"),
            "title": record.get("title"),
            "abstract": abstract,
            "published_date": record.get("published_date"),
            "listed_date": record.get("listed_date"),
            "posted_date": record.get("listed_date"),
            "authors": record.get("authors") or "",
            "publisher": record.get("publisher") or "",
            "department": record.get("department") or "",
            "journal": record.get("journal") or "",
            "url": record.get("url"),
            "pdf_url": record.get("pdf_url"),
            "keywords": record.get("keywords") or "",
            "category": record.get("category") or "PDF dataset",
            "doi": record.get("doi") or "",
            "original_filename": record.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }
        self._save_paper(paper)
        return True

    def _build_record(self, item, slug):
        rss = item.get("rss") or {}
        subject = self._lookup_dataset_subject(slug)
        triples = self._triples_for_subject(subject) if subject else []

        list_title = item.get("title") or ""
        list_abstract = item.get("abstract") or ""
        title = self._choose_literal(triples, DCT_TITLE) or list_title

        descriptions = self._literal_values(triples, DCT_DESCRIPTION)
        abstract = self._choose_best_description(descriptions, list_abstract)
        citation = self._choose_literal(triples, DCT_BIBLIO)
        if citation and citation not in abstract:
            abstract = self._clean_text(f"{abstract} {citation}")

        identifiers = self._values(triples, DCT_IDENTIFIER)
        external_id = self._first_nonempty(identifiers) or slug
        doi = self._extract_doi(triples, subject, slug)

        listed_date = rss.get("listed_date")
        if not listed_date:
            listed_date = self._parse_date(self._first_nonempty(self._values(triples, DCT_MODIFIED)))

        published_date = (
            self._parse_date(self._first_nonempty(self._values(triples, DCT_ISSUED)))
            or self._parse_date(self._first_nonempty(self._values(triples, DCT_CREATED)))
            or self._parse_date(self._first_nonempty(self._values(triples, SCHEMA_START_DATE)))
            or listed_date
        )

        publisher_uris = self._uri_values(triples, DCT_PUBLISHER)
        contact_uris = self._uri_values(triples, DCAT_CONTACT_POINT)
        creator_uris = self._uri_values(triples, DCT_CREATOR)
        publishers = self._labels_for_uris(publisher_uris + contact_uris)
        authors = self._labels_for_uris(creator_uris)
        if not authors and rss.get("creator"):
            authors = [rss["creator"]]

        keywords = self._dedupe(self._literal_values(triples, DCAT_KEYWORD))
        categories = self._category_values(triples)
        distributions = self._resolve_distributions(self._uri_values(triples, DCAT_DISTRIBUTION))
        pdf_url = None
        original_filename = None
        for dist in distributions:
            candidate = dist.get("downloadURL") or dist.get("accessURL")
            if candidate and self._looks_like_pdf(candidate, dist):
                pdf_url = candidate
                original_filename = self._filename_from_url(candidate)
                break
        if pdf_url is None and distributions:
            for dist in distributions:
                candidate = dist.get("downloadURL") or dist.get("accessURL")
                if candidate:
                    pdf_url = candidate
                    original_filename = self._filename_from_url(candidate)
                    break

        post_number = self._post_number(slug, rss.get("node_id") or item.get("node_id"))
        raw_dataset = self._triples_to_metadata(triples, subject)
        metadata = {
            "raw_list_item": dict(item),
            "rss": rss,
            "dataset_subject": subject,
            "dataset_triples": raw_dataset,
            "distributions": distributions,
            "identifier_values": identifiers,
            "publisher_uris": publisher_uris,
            "creator_uris": creator_uris,
            "contact_uris": contact_uris,
            "category_values": categories,
            "node_id": rss.get("node_id") or item.get("node_id"),
            "slug": slug,
            "doi_slug_decoded": self._doi_from_slug(slug),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date_raw": rss.get("pubDate"),
            "authors": "; ".join(authors),
            "publisher": "; ".join(publishers),
            "department": "",
            "journal": "",
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "url": item.get("url"),
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": "; ".join(categories) if categories else "PDF dataset",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
            "node_id": rss.get("node_id") or item.get("node_id"),
        }

    # ------------------------------------------------------------------
    # LDF / N-Triples helpers
    # ------------------------------------------------------------------

    def _lookup_dataset_subject(self, slug):
        if not slug:
            return None
        if slug in self._subject_cache:
            return self._subject_cache[slug]

        subjects = []
        for identifier in self._identifier_candidates(slug):
            triples = self._fetch_ldf(
                {
                    "predicate": DCT_IDENTIFIER,
                    "object": f'"{identifier}"',
                }
            )
            for triple in triples:
                if triple["predicate"] == DCT_IDENTIFIER and triple["value"] == identifier:
                    subjects.append(triple["subject"])

        if not subjects:
            doi = self._doi_from_slug(slug)
            if doi:
                doi_subject = f"https://doi.org/{doi}"
                triples = self._fetch_ldf({"subject": doi_subject})
                subject_triples = [t for t in triples if t["subject"] == doi_subject]
                meaningful = [t for t in subject_triples if t["predicate"] != RDF_TYPE]
                if meaningful:
                    subjects.append(doi_subject)
                    self._triples_cache[doi_subject] = subject_triples

        subject = subjects[0] if subjects else None
        self._subject_cache[slug] = subject
        return subject

    def _triples_for_subject(self, subject):
        if not subject:
            return []
        if subject in self._triples_cache:
            return [t for t in self._triples_cache[subject] if t["subject"] == subject]
        triples = self._fetch_ldf({"subject": subject})
        triples = [t for t in triples if t["subject"] == subject]
        self._triples_cache[subject] = triples
        return triples

    def _resolve_distributions(self, uris):
        out = []
        for uri in self._dedupe(uris):
            triples = self._triples_for_subject(uri)
            title = self._choose_literal(triples, DCT_TITLE)
            format_values = self._values(triples, DCT_FORMAT)
            media_values = self._values(triples, DCAT_MEDIA_TYPE)
            download = self._first_nonempty(self._uri_values(triples, DCAT_DOWNLOAD_URL))
            access = self._first_nonempty(self._uri_values(triples, DCAT_ACCESS_URL))
            out.append(
                {
                    "uri": uri,
                    "title": title,
                    "format": format_values,
                    "mediaType": media_values,
                    "downloadURL": download,
                    "accessURL": access,
                    "raw": self._triples_to_metadata(triples, uri),
                }
            )
        return out

    def _parse_ntriples(self, raw):
        triples = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(\S+)\s+<([^>]+)>\s+(.*)\s+\.$", line, flags=re.S)
            if not match:
                continue
            subject_token, predicate, object_raw = match.groups()
            subject = self._parse_subject_token(subject_token)
            obj = self._parse_object_token(object_raw)
            if not subject or not obj:
                continue
            triples.append(
                {
                    "subject": subject,
                    "predicate": predicate,
                    "kind": obj["kind"],
                    "value": obj["value"],
                    "lang": obj.get("lang"),
                    "datatype": obj.get("datatype"),
                }
            )
        return triples

    @staticmethod
    def _parse_subject_token(token):
        if token.startswith("<") and token.endswith(">"):
            return token[1:-1]
        if token.startswith("_:"):
            return token
        return None

    def _parse_object_token(self, raw):
        raw = raw.strip()
        if raw.startswith("<"):
            end = raw.find(">")
            if end > 0:
                return {"kind": "uri", "value": raw[1:end]}
            return None
        if raw.startswith("_:"):
            return {"kind": "bnode", "value": raw.split()[0]}
        if raw.startswith('"'):
            match = re.match(r'^"((?:\\.|[^"\\])*)"(.*)$', raw, flags=re.S)
            if not match:
                return None
            value = self._unescape_nt_literal(match.group(1))
            rest = match.group(2).strip()
            lang = None
            datatype = None
            lang_match = re.match(r"^@([A-Za-z0-9-]+)", rest)
            if lang_match:
                lang = lang_match.group(1)
            dtype_match = re.search(r"\^\^<([^>]+)>", rest)
            if dtype_match:
                datatype = dtype_match.group(1)
            return {
                "kind": "literal",
                "value": value,
                "lang": lang,
                "datatype": datatype,
            }
        return None

    @staticmethod
    def _unescape_nt_literal(value):
        def replace_unicode(match):
            token = match.group(0)
            try:
                if token.startswith("\\U"):
                    return chr(int(token[2:], 16))
                return chr(int(token[2:], 16))
            except ValueError:
                return token

        value = re.sub(r"\\U[0-9A-Fa-f]{8}|\\u[0-9A-Fa-f]{4}", replace_unicode, value)
        replacements = {
            r"\t": "\t",
            r"\n": "\n",
            r"\r": "\r",
            r"\"": '"',
            r"\\": "\\",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    def _values(self, triples, predicate):
        return [t["value"] for t in triples if t["predicate"] == predicate]

    def _literal_values(self, triples, predicate):
        return [t["value"] for t in triples if t["predicate"] == predicate and t["kind"] == "literal"]

    def _uri_values(self, triples, predicate):
        return [t["value"] for t in triples if t["predicate"] == predicate and t["kind"] == "uri"]

    def _choose_literal(self, triples, predicate):
        candidates = [t for t in triples if t["predicate"] == predicate and t["kind"] == "literal"]
        if not candidates:
            return None
        preferred = ("nl", "nl-t-en", "en", "fr", "de", None)
        for lang in preferred:
            matching = [t["value"] for t in candidates if (t.get("lang") or None) == lang]
            if matching:
                return self._clean_text(max(matching, key=len))
        return self._clean_text(max([t["value"] for t in candidates], key=len))

    def _choose_best_description(self, descriptions, list_abstract):
        cleaned = [self._clean_text(v) for v in descriptions if self._clean_text(v)]
        if cleaned:
            abstract = max(cleaned, key=len)
        else:
            abstract = self._clean_text(list_abstract or "")
        if list_abstract:
            list_clean = self._clean_text(list_abstract)
            if list_clean and list_clean not in abstract:
                if len(abstract) < _ABSTRACT_MIN_SAVE or len(list_clean) > len(abstract):
                    abstract = self._clean_text(f"{abstract} {list_clean}")
        return abstract

    def _labels_for_uris(self, uris):
        labels = []
        for uri in self._dedupe(uris):
            label = self._label_for_uri(uri)
            if label:
                labels.append(label)
        return self._dedupe(labels)

    def _label_for_uri(self, uri):
        if not uri:
            return None
        if uri in self._label_cache:
            return self._label_cache[uri]
        triples = self._triples_for_subject(uri)
        for predicate in (SKOS_PREF_LABEL, RDFS_LABEL, FOAF_NAME, VCARD_FN, VCARD_ORG_NAME, DCT_TITLE):
            label = self._choose_literal(triples, predicate)
            if label:
                self._label_cache[uri] = label
                return label
        given = self._choose_literal(triples, FOAF_GIVEN_NAME)
        family = self._choose_literal(triples, FOAF_FAMILY_NAME)
        if given or family:
            label = self._clean_text(f"{given or ''} {family or ''}")
        else:
            label = self._uri_tail(uri)
        self._label_cache[uri] = label
        return label

    def _category_values(self, triples):
        values = []
        for predicate in (DCT_TYPE, DCAT_THEME, DCT_FORMAT):
            for value in self._values(triples, predicate):
                if value == _FORMAT_URI:
                    values.append("PDF")
                else:
                    values.append(self._uri_tail(value))
        return self._dedupe([v for v in values if v])

    def _extract_doi(self, triples, subject, slug):
        candidates = []
        if subject:
            candidates.append(subject)
        candidates.extend(self._values(triples, ADMS_IDENTIFIER))
        candidates.extend(self._values(triples, DCAT_LANDING_PAGE))
        doi_from_slug = self._doi_from_slug(slug)
        if doi_from_slug:
            candidates.append(doi_from_slug)
        for value in candidates:
            doi = self._normalize_doi(value)
            if doi:
                return doi
        return None

    @staticmethod
    def _normalize_doi(value):
        if not value:
            return None
        value = str(value).strip()
        match = re.search(r"(10\.\d{4,9}/[^\s<>\"]+)", value, flags=re.I)
        if match:
            return match.group(1).rstrip(".,;)")
        return None

    @staticmethod
    def _doi_from_slug(slug):
        if not slug:
            return None
        slug_l = slug.lower()
        if not slug_l.startswith("doi10"):
            return None
        rest = slug_l[3:]
        for marker in ("dvn", "ulg"):
            idx = rest.find(marker)
            if idx > 2:
                prefix = rest[:idx]
                suffix = rest[idx + len(marker):]
                if prefix.startswith("10") and suffix:
                    return f"{prefix[:2]}.{prefix[2:]}/{marker.upper()}/{suffix.upper()}"
        match = re.match(r"^(10)(\d{4,9})([a-z]+)([a-z0-9]+)$", rest)
        if match:
            return f"10.{match.group(2)}/{match.group(3).upper()}/{match.group(4).upper()}"
        return None

    def _identifier_candidates(self, slug):
        candidates = [slug]
        doi = self._doi_from_slug(slug)
        if doi:
            candidates.extend([doi, f"https://doi.org/{doi}"])
        return self._dedupe(candidates)

    @staticmethod
    def _looks_like_pdf(url, dist):
        lowered = (url or "").lower()
        if ".pdf" in lowered or lowered.endswith("/pdf"):
            return True
        for value in dist.get("format") or []:
            if str(value).lower().endswith("/pdf") or "pdf" in str(value).lower():
                return True
        for value in dist.get("mediaType") or []:
            if "pdf" in str(value).lower():
                return True
        return False

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _post_number(slug, node_id):
        if node_id and str(node_id).isdigit():
            return str(node_id)
        match = re.search(r"\d+", slug or "")
        if match and len(match.group(0)) >= 4:
            return match.group(0)
        return slug or None

    def _triples_to_metadata(self, triples, subject=None):
        grouped = {}
        for triple in triples:
            if subject and triple["subject"] != subject:
                continue
            key = triple["predicate"]
            grouped.setdefault(key, []).append(
                {
                    "value": triple["value"],
                    "kind": triple["kind"],
                    "lang": triple.get("lang"),
                    "datatype": triple.get("datatype"),
                }
            )
        return grouped

    # ------------------------------------------------------------------
    # General parsing utilities
    # ------------------------------------------------------------------

    def _make_soup(self, raw, prefer_xml=False):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable: {exc}")
            return None

        parsers = ["xml", "html5lib", "lxml", "html.parser"] if prefer_xml else ["html5lib", "lxml", "html.parser"]
        for parser in parsers:
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
                continue
        return None

    @staticmethod
    def _tag_text(node, name):
        tag = node.find(name)
        if tag is None and ":" in name:
            tag = node.find(name.split(":", 1)[1])
        if tag is None:
            return None
        return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip() or None

    @staticmethod
    def _clean_text(value):
        if not value:
            return ""
        value = re.sub(r"\s+", " ", str(value)).strip()
        return value.replace(" (...)", "").replace("(…)", "").strip()

    @staticmethod
    def _slug_from_url(url):
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        if not path:
            return None
        return unquote(path.split("/")[-1])

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        raw = str(raw).strip()
        if not raw:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if match:
            return "-".join(match.groups())
        match = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
        if match:
            return "-".join(match.groups())
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt:
                return dt.date().isoformat()
        except Exception:
            pass
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(raw[:10], fmt).date().isoformat()
            except ValueError:
                continue
        return None

    @staticmethod
    def _uri_tail(value):
        if not value:
            return None
        value = str(value).rstrip("/")
        if "#" in value:
            value = value.rsplit("#", 1)[-1]
        else:
            value = value.rsplit("/", 1)[-1]
        return unquote(value).replace("_", " ").strip() or None

    @staticmethod
    def _first_nonempty(values):
        for value in values or []:
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _dedupe(values):
        out = []
        seen = set()
        for value in values or []:
            if value in (None, ""):
                continue
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return out

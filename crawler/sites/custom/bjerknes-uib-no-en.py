# -*- coding: utf-8 -*-
"""Crawler for Bjerknes Centre publications.

The public page is a Next.js app backed by Craft CMS GraphQL:
https://craft.bjerknes.uib.no/graphql

Publication entries on the Bjerknes site contain bibliographic metadata but no
abstract field, so DOI records are enriched from CrossRef when possible.
"""

import json
import re
import subprocess
import time
from html import unescape
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


GRAPHQL_ENDPOINT = "https://craft.bjerknes.uib.no/graphql"
CROSSREF_WORKS_ENDPOINT = "https://api.crossref.org/works"
PAGE_SIZE = 50
MAX_PAGES = 200
MAX_SECONDS = 25 * 60
MIN_ABSTRACT_CHARS = 100


PUBLICATIONS_QUERY = """
query BjerknesPublications($site: [String]!, $limit: Int!, $offset: Int!) {
  entries(
    site: $site
    section: "publications"
    limit: $limit
    offset: $offset
    orderBy: "postDate DESC"
  ) {
    __typename
    id
    uid
    title
    slug
    uri
    url
    postDate
    dateCreated
    dateUpdated
    siteHandle
    language
    enabled
    status
    ... on sectionPublications_Entry {
      year: sectionPublicationsPublicationYear
      fullAuthorList: sectionPublicationsFullAuthorList
      jurnal: sectionPublicationsJurnal
      source: sectionPublicationsSource
      relatedGroups: sectionPublicationsRelatedResearchGroups {
        id
        title
      }
      relatedFields: sectionPublicationsRelatedFieldOfResearch {
        id
        title
      }
      relatedProjects: sectionPublicationsRelatedProjects {
        id
        title
      }
      researchers: sectionPublicationsBccrResearchers {
        id
        title
        firstName: researcherFirstName
        lastName: researcherLastName
        institution: researcherInstitution {
          id
          title
        }
      }
    }
  }
}
"""


def _curl(args, retries=3):
    """Run curl and return decoded stdout, retrying transient failures."""
    wait_times = [1, 3, 9]
    cmd = ["curl", "-skL", "--tls-max", "1.3", "--max-time", "30"] + args
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and body.strip():
                return body
            err = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt < retries - 1:
                wait = wait_times[attempt]
                print(
                    f"[bjerknes-uib-no-en] curl failed "
                    f"(attempt {attempt + 1}/{retries}), retry in {wait}s: {err[:160]}"
                )
                time.sleep(wait)
            else:
                print(
                    f"[bjerknes-uib-no-en] curl failed after {retries} attempts: "
                    f"{err[:200] or 'empty response'}"
                )
        except Exception as exc:
            if attempt < retries - 1:
                wait = wait_times[attempt]
                print(
                    f"[bjerknes-uib-no-en] curl error "
                    f"(attempt {attempt + 1}/{retries}), retry in {wait}s: {exc}"
                )
                time.sleep(wait)
            else:
                print(f"[bjerknes-uib-no-en] curl failed after {retries} attempts: {exc}")
    return None


def _curl_json_post(url, payload):
    raw = _curl([
        "-X", "POST",
        "-H", "Content-Type: application/json",
        "-H", f"User-Agent: {BaseCrawler.USER_AGENT}",
        "--data-binary", json.dumps(payload, ensure_ascii=False),
        url,
    ])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[bjerknes-uib-no-en] JSON decode failed for POST {url}: {exc}")
        return None


def _curl_json_get(url):
    raw = _curl([
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {BaseCrawler.USER_AGENT}",
        url,
    ])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[bjerknes-uib-no-en] JSON decode failed for GET {url}: {exc}")
        return None


def _curl_text_get(url):
    return _curl([
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", f"User-Agent: {BaseCrawler.USER_AGENT}",
        url,
    ])


def _soup(raw):
    """Parse HTML with html5lib, then lxml, then html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _text_from_html(raw):
    if not raw:
        return ""
    soup = _soup(raw)
    if soup is not None:
        try:
            return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        except Exception:
            pass
    text = re.sub(r"<[^>]+>", " ", raw)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    match = re.search(r"(\d{4})", text)
    if match:
        return f"{match.group(1)}-01-01"
    return None


def _date_parts_to_iso(value):
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts") or []
    if not parts or not isinstance(parts[0], list) or not parts[0]:
        return None
    nums = parts[0]
    try:
        year = int(nums[0])
        month = int(nums[1]) if len(nums) > 1 else 1
        day = int(nums[2]) if len(nums) > 2 else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return None


def _first_list_value(value):
    if isinstance(value, list) and value:
        return value[0]
    return None


def _extract_doi(value):
    if not value:
        return None
    match = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", str(value), re.I)
    if not match:
        return None
    doi = match.group(1).strip()
    return doi.rstrip(".,);]").lower()


def _doi_url(doi):
    if not doi:
        return None
    return f"https://doi.org/{doi}"


def _authors_from_crossref(message):
    authors = []
    for author in message.get("author") or []:
        given = (author.get("given") or "").strip()
        family = (author.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part)
        if not name:
            name = (author.get("name") or "").strip()
        if name:
            authors.append(name)
    return "; ".join(authors) if authors else None


def _authors_from_site(raw):
    if not raw:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip()
    text = re.sub(r"\s+\band\b\s+", "; ", text)
    text = text.replace(" & ", "; ")
    parts = [p.strip(" ,") for p in text.split(";") if p.strip(" ,")]
    return "; ".join(parts) if parts else text


def _names(items):
    values = []
    for item in items or []:
        title = item.get("title") if isinstance(item, dict) else None
        if title:
            values.append(re.sub(r"\s+", " ", str(title)).strip())
    return values


def _researcher_institutions(item):
    values = []
    for researcher in item.get("researchers") or []:
        for inst in researcher.get("institution") or []:
            title = inst.get("title")
            if title:
                values.append(re.sub(r"\s+", " ", str(title)).strip())
    deduped = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _pdf_filename(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(PurePosixPath(path).name or "").strip()
    if tail and "." in tail and len(tail) <= 200:
        return tail
    doi = _extract_doi(url)
    if doi:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doi.split("/", 1)[-1]).strip("_")
        return f"{safe or 'publication'}.pdf"
    return None


def _select_pdf_url(message):
    for link in message.get("link") or []:
        if not isinstance(link, dict):
            continue
        content_type = (link.get("content-type") or "").lower()
        url = link.get("URL")
        if url and "pdf" in content_type:
            return url
    return None


def _crossref_published_date(message):
    for key in ("published", "published-print", "published-online", "issued", "created"):
        date = _date_parts_to_iso(message.get(key))
        if date:
            return date
    return None


def _crossref_detail(doi):
    if not doi:
        return {}
    url = f"{CROSSREF_WORKS_ENDPOINT}/{quote(doi, safe='/')}"
    data = _curl_json_get(url)
    if not isinstance(data, dict):
        return {}
    message = data.get("message")
    return message if isinstance(message, dict) else {}


def _meta_content(soup, names):
    for name in names:
        for attr in ("name", "property"):
            try:
                tag = soup.find("meta", attrs={attr: name})
                content = tag.get("content") if tag else ""
                text = re.sub(r"\s+", " ", unescape(content or "")).strip()
                if text:
                    return text
            except Exception:
                continue
    return ""


def _html_metadata(url):
    if not url:
        return {}
    raw = _curl_text_get(url)
    if not raw:
        return {}
    soup = _soup(raw)
    if soup is None:
        return {}

    abstract = _meta_content(
        soup,
        ["citation_abstract", "dc.description", "DC.Description", "description", "og:description"],
    )
    if len(abstract) < MIN_ABSTRACT_CHARS:
        for selector in ("section#Abs1", "section[aria-labelledby='Abs1']", "#Abs1-content"):
            try:
                section = soup.select_one(selector)
                text = (
                    re.sub(r"\s+", " ", section.get_text(" ", strip=True)).strip()
                    if section
                    else ""
                )
                if len(text) >= MIN_ABSTRACT_CHARS:
                    abstract = text
                    break
            except Exception:
                continue

    pdf_url = _meta_content(soup, ["citation_pdf_url"])
    if pdf_url:
        pdf_url = urljoin(url, pdf_url)

    published = _meta_content(
        soup,
        ["citation_publication_date", "dc.date", "DC.Date", "article:published_time"],
    )

    return {
        "abstract": abstract,
        "pdf_url": pdf_url,
        "published_date": _iso_date(published),
    }


class BjerknesUibNoEnCrawler(BaseCrawler):
    site_id = "bjerknes-uib-no-en"
    site_name = "Custom: bjerknes-uib-no-en"
    base_url = "https://bjerknes.uib.no"

    def _fetch_page(self, page):
        offset = (page - 1) * PAGE_SIZE
        payload = {
            "query": PUBLICATIONS_QUERY,
            "variables": {"site": ["en"], "limit": PAGE_SIZE, "offset": offset},
        }
        data = _curl_json_post(GRAPHQL_ENDPOINT, payload)
        if not isinstance(data, dict):
            return None
        if data.get("errors"):
            print(f"[bjerknes-uib-no-en] GraphQL errors on page {page}: {data['errors']}")
            return None
        entries = ((data.get("data") or {}).get("entries") or [])
        return entries if isinstance(entries, list) else None

    def _build_paper(self, item):
        craft_id = str(item.get("id") or "").strip()
        if not craft_id:
            return None

        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if not title:
            return None

        source = (item.get("source") or "").strip()
        doi = _extract_doi(source)
        detail = _crossref_detail(doi) if doi else {}

        html_detail = {}
        abstract = _text_from_html(detail.get("abstract") or "")
        if len(abstract) < MIN_ABSTRACT_CHARS:
            html_detail = _html_metadata(_doi_url(doi) or source)
            abstract = html_detail.get("abstract") or ""
        if len(abstract) < MIN_ABSTRACT_CHARS:
            print(f"[bjerknes-uib-no-en] item {craft_id} skipped: abstract too short ({len(abstract)} chars)")
            return None

        listed_date_raw = item.get("postDate")
        listed_date = _iso_date(listed_date_raw)
        published_date = (
            _crossref_published_date(detail)
            or html_detail.get("published_date")
            or _iso_date(item.get("year"))
            or listed_date
        )

        authors = _authors_from_crossref(detail) or _authors_from_site(item.get("fullAuthorList"))
        publisher = detail.get("publisher")
        if not publisher:
            institutions = _researcher_institutions(item)
            publisher = "; ".join(institutions) if institutions else None

        journal = (
            _first_list_value(detail.get("container-title"))
            or _first_list_value(detail.get("short-container-title"))
            or item.get("jurnal")
        )
        pdf_url = _select_pdf_url(detail) or html_detail.get("pdf_url")
        original_filename = _pdf_filename(pdf_url)

        related_groups = _names(item.get("relatedGroups"))
        related_fields = _names(item.get("relatedFields"))
        related_projects = _names(item.get("relatedProjects"))
        department_values = related_groups or related_fields
        keywords_values = related_fields or (detail.get("subject") or [])

        meta_url = (
            (detail.get("resource") or {}).get("primary", {}).get("URL")
            if isinstance(detail.get("resource"), dict)
            else None
        )
        url = meta_url or detail.get("URL") or _doi_url(doi) or source or f"{GRAPHQL_ENDPOINT}#entry-{craft_id}"

        metadata = {
            "posted_date": listed_date_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": item.get("jurnal"),
            "series": detail.get("container-title"),
            "volume": detail.get("volume"),
            "issue": detail.get("issue"),
            "node_id": craft_id,
            "craft_id": craft_id,
            "uid": item.get("uid"),
            "slug": item.get("slug"),
            "post_number": craft_id,
            "source": source,
            "craft_postDate": item.get("postDate"),
            "craft_dateCreated": item.get("dateCreated"),
            "craft_dateUpdated": item.get("dateUpdated"),
            "craft_year": item.get("year"),
            "craft_url": item.get("url"),
            "craft_uri": item.get("uri"),
            "siteHandle": item.get("siteHandle"),
            "language": item.get("language"),
            "status": item.get("status"),
            "relatedGroups": item.get("relatedGroups"),
            "relatedFields": item.get("relatedFields"),
            "relatedProjects": item.get("relatedProjects"),
            "researchers": item.get("researchers"),
            "craft_entry": item,
            "publisher_html": html_detail or None,
            "crossref": {
                "DOI": detail.get("DOI"),
                "type": detail.get("type"),
                "publisher": detail.get("publisher"),
                "published": detail.get("published"),
                "published_print": detail.get("published-print"),
                "published_online": detail.get("published-online"),
                "URL": detail.get("URL"),
                "page": detail.get("page"),
                "ISSN": detail.get("ISSN"),
                "ISBN": detail.get("ISBN"),
                "subject": detail.get("subject"),
                "link": detail.get("link"),
            } if detail else None,
        }

        return {
            "id": f"{self.site_id}:{craft_id}",
            "site_id": self.site_id,
            "external_id": craft_id,
            "post_number": craft_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": "; ".join(department_values) if department_values else None,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(str(x).strip() for x in keywords_values if str(x).strip()) or None,
            "category": detail.get("type") or "publication",
            "doi": (detail.get("DOI") or doi) if doi else None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 1
        seen_urls = set()
        start = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[bjerknes-uib-no-en] safety cap of {MAX_PAGES} pages reached")
                break
            if time.time() - start > MAX_SECONDS - 5:
                print(f"[bjerknes-uib-no-en] approaching 25-minute budget at page {page}; stopping")
                break
            if page % 10 == 0:
                print(f"[bjerknes-uib-no-en] page {page}: saved {saved}/{limit_or_inf}")

            items = self._fetch_page(page)
            if items is None:
                print(f"[bjerknes-uib-no-en] page {page}: fetch failed; stopping")
                break
            if not items:
                print(f"[bjerknes-uib-no-en] page {page}: no records; stopping")
                break

            page_new = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start > MAX_SECONDS - 5:
                    print("[bjerknes-uib-no-en] approaching 25-minute budget during item loop; stopping")
                    return saved

                item_id = str(item.get("id") or "?")
                dedupe_url = (
                    item.get("source")
                    or item.get("url")
                    or item.get("uid")
                    or f"{GRAPHQL_ENDPOINT}#entry-{item_id}"
                )
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                page_new += 1

                try:
                    time.sleep(self._delay)
                    paper = self._build_paper(item)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[bjerknes-uib-no-en] item {item_id} failed: {exc}")
                    continue

            if page_new == 0:
                print(f"[bjerknes-uib-no-en] page {page}: 0 new records; stopping")
                break
            page += 1

        return saved

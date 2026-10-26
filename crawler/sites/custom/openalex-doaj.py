# -*- coding: utf-8 -*-
"""OpenAlex works in DOAJ journals (articles and reviews), for the plan-driven collector.

Enumeration only (strategy ``api_cursor``; adapter contract with list and paper methods, no detail
request): the framework requests ``https://api.openalex.org/works`` itself (gate, robots, budget; the
API key is a request header the plan names, never in a URL) and hands each JSON page to ``_enumeration_list``,
which turns every work into the document the bulk snapshot import produced for it: the same
post_number (short work id ``W...``), meta_url (``https://doi.org/<doi>``, else the OpenAlex page),
title, dates, authors, journal, publisher, OA PDF link, keywords line and restored abstract. The
same work therefore keeps the identity of its bulk row, and the importer skips it as existing.

A work without a title is skipped by rule (``_enumeration_paper`` returns None; the runner ledgers
it as ``skipped_by_rule``, counted in the report), as the snapshot export skipped such works. A work
outside the query's own filter (type other than article/review, or a primary source not in DOAJ)
means the filter was not applied: the listing stops as a configuration error instead of silently
dropping it.

Scope: the query keeps works whose primary source is in DOAJ *now* (``is_in_doaj``); the bulk
export used a fixed list of DOAJ source ids from its snapshot, so a journal that joined or left DOAJ
since then is in one and not the other. Like the bulk rows, nothing here drops works whose DOI is
already delivered under another site or shared by two works: the import step does that.

Deleted and merged works: this collector never deletes anything. A merged work is saved under the
id the API returns for it (the surviving work). Works OpenAlex removed are handled outside
collection: reported only, and removed from delivered data only with the user's approval.

``crawl()`` (the legacy crawler loop) does not collect this source.
"""
from __future__ import annotations

from crawler.base_crawler import BaseCrawler
from crawler.enumeration.adapters import AdapterConfigError

KEEP_TYPES = ("article", "review")
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")


def norm_doi(doi):
    if not doi:
        return None
    value = doi.strip().lower()
    for prefix in DOI_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value or None


def restore_abstract(inverted):
    """OpenAlex abstract_inverted_index (word -> positions) back to the text."""
    if not inverted:
        return None
    words = {}
    for word, places in inverted.items():
        for place in places:
            words[place] = word
    return " ".join(words[i] for i in sorted(words)) or None


def slim(work):
    """The record the bulk snapshot filter kept for one work."""
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    best = work.get("best_oa_location") or {}
    return {
        "id": work["id"].rsplit("/", 1)[-1],
        "doi": work.get("doi"),
        "title": work.get("title") or work.get("display_name"),
        "publication_date": work.get("publication_date"),
        "publication_year": work.get("publication_year"),
        "language": work.get("language"),
        "type": work.get("type"),
        "authors": [(a.get("author") or {}).get("display_name") for a in (work.get("authorships") or [])
                    if (a.get("author") or {}).get("display_name")],
        "journal": source.get("display_name"),
        "source_id": (source.get("id") or "").rsplit("/", 1)[-1],
        "issn_l": source.get("issn_l"),
        "publisher": source.get("host_organization_name"),
        "landing_page_url": primary.get("landing_page_url"),
        "pdf_url": best.get("pdf_url") or primary.get("pdf_url"),
        "oa_url": (work.get("open_access") or {}).get("oa_url"),
        "license": best.get("license") or primary.get("license"),
        "keywords": [k.get("display_name") for k in (work.get("keywords") or []) if k.get("display_name")],
        "abstract": restore_abstract(work.get("abstract_inverted_index")),
        "updated_date": work.get("updated_date"),
    }


def paper(record):
    """Listing row / paper for one slim record: the bulk export's document() columns."""
    doi = norm_doi(record.get("doi"))
    keywords = list(record.get("keywords") or [])
    if doi:
        keywords.append("DOI:" + doi)
    if record.get("issn_l"):
        keywords.append("ISSN-L:" + record["issn_l"])
    if record.get("language"):
        keywords.append("lang:" + record["language"])
    keywords.append("source:OpenAlex (CC0)")
    return {
        "site_id": OpenAlexDoajCrawler.site_id,
        "external_id": record["id"],
        # The listing URL is the work's stable OpenAlex address (the ledger compares W ids, so a DOI
        # added later is not a listing conflict); the saved meta_url is the bulk rows' one.
        "url": "https://openalex.org/" + record["id"],
        "meta_url": ("https://doi.org/" + doi) if doi else ("https://openalex.org/" + record["id"]),
        "title": (record.get("title") or "").strip(),
        "published_date": record.get("publication_date"),
        "authors": list(record.get("authors") or []),
        "publisher": record.get("publisher"),
        "journal": record.get("journal"),
        "pdf_url": record.get("pdf_url"),
        "keywords": "; ".join(keywords),
        "abstract": record.get("abstract"),
        "doi": doi,
        "updated_date": record.get("updated_date"),
    }


class OpenAlexDoajCrawler(BaseCrawler):
    site_id = "openalex-doaj"
    site_name = "OpenAlex (CC0), DOAJ 등재 학술지"
    base_url = "https://openalex.org"
    sheet = "OpenAlex"

    def crawl(self, limit=None):
        self._report_source_failure("configuration")  # collected only by the plan-driven collector
        return 0

    def _enumeration_list(self, data):
        """Rows (papers) of one /works page."""
        rows = []
        for work in (data or {}).get("results") or []:
            if not isinstance(work, dict) or not work.get("id"):
                raise AdapterConfigError("openalex_work_without_id")
            source = (work.get("primary_location") or {}).get("source") or {}
            if work.get("type") not in KEEP_TYPES or source.get("is_in_doaj") is not True:
                raise AdapterConfigError("openalex_work_outside_filter")
            rows.append(paper(slim(work)))
        return rows

    def _enumeration_paper(self, row):
        """The listing row is the paper; a work without a title is skipped (None), as in the bulk export."""
        return row if (row.get("title") or "").strip() else None

    def _save_paper(self, paper_dict):
        """BaseCrawler._save_paper with the bulk rows' exact columns: the shared adapter would re-split
        keywords and publishers on commas and derive an original_filename from the PDF link."""
        paper_dict.setdefault("site_id", self.site_id)
        return self._save_paper_v2({
            "site_id": paper_dict["site_id"],
            "post_number": paper_dict.get("external_id"),
            "meta_url": paper_dict.get("meta_url") or paper_dict.get("url"),
            "title": paper_dict.get("title"),
            "published_date": paper_dict.get("published_date"),
            "listed_date": None,
            "authors": "; ".join(paper_dict.get("authors") or []) or None,
            "publisher": paper_dict.get("publisher"),
            "journal": paper_dict.get("journal"),
            "pdf_url": paper_dict.get("pdf_url"),
            "keywords": paper_dict.get("keywords") or None,
            "abstract": paper_dict.get("abstract"),
        })

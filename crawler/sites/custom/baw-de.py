"""Keep the legacy BAW annual-report ID using the active publications API."""
from importlib import import_module

_reports = import_module("crawler.sites.custom.baw-de-en")


class BawDeLegacyCrawler(_reports.BawDeEnCrawler):
    site_id = "baw-de"
    site_name = "BAW — Annual Reports"

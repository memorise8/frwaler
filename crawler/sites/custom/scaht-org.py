"""Keep the legacy SCAHT publication ID on the current paginated listing."""
from importlib import import_module

_publications = import_module("crawler.sites.custom.scaht-org-en")


class ScahtOrgLegacyCrawler(_publications.SCAHTOrgEnCrawler):
    site_id = "scaht-org"
    site_name = "SCAHT — Publications"
    START_URL = _publications.SCAHTOrgEnCrawler.LIVE_LIST_URL

"""Keep the legacy ARCEP annual-report ID on the maintained browser crawler."""
from importlib import import_module

_publications = import_module("crawler.sites.custom.arcep-fr-actualites")


class ArcepFrLegacyCrawler(_publications.ArcepFrActualitesCrawler):
    site_id = "arcep-fr"
    site_name = "ARCEP — Rapports annuels"

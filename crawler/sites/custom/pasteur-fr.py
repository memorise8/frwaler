"""Preserve the legacy Pasteur ID with the current press-release parser."""
from importlib import import_module

_press = import_module("crawler.sites.custom.pasteur-fr-en")


class PasteurFrLegacyCrawler(_press.PasteurFrEnCrawler):
    site_id = "pasteur-fr"
    site_name = "Institut Pasteur — Press releases"
